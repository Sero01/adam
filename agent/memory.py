"""SQLite memory: schema, notes, episodic log, working context, compaction."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config

if TYPE_CHECKING:
    from .llm import LLM

log = logging.getLogger("memory")

PROMPTS = Path(__file__).parent / "prompts"
SECTIONS = ["Current focus", "What I've done", "What I know", "Open threads", "Notes to self"]
PROTECTED = ("Current focus", "Open threads")
RENDER_ITEM_CAP = 2000     # per tool result / message in rendered turns
COMPACT_CHUNK_TOKENS = 24000
# Note 0 is the compactor's state summary (read-only to the Thinker). Note 1 is the Thinker's
# scratch note: shown every turn, never touched by the compactor. Before this split the Thinker
# rewrote note 0 free-form and each compaction silently reformatted it (P4 run 3).
SCRATCH_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY, ts REAL, title TEXT, body TEXT, tags TEXT, updated_ts REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, body, tags, content='notes', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags); END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
  INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags); END;

CREATE TABLE IF NOT EXISTS ticks (
  id INTEGER PRIMARY KEY, ts REAL, elapsed_s REAL, goal TEXT, goal_changed INTEGER,
  status TEXT, n_tool_calls INTEGER DEFAULT 0, n_hands_calls INTEGER DEFAULT 0,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, tokens_cached INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, slept_s REAL DEFAULT 0, human_in INTEGER DEFAULT 0,
  human_out INTEGER DEFAULT 0, aborted INTEGER DEFAULT 0,
  -- additions beyond the spec's column list
  end_tick_called INTEGER DEFAULT 0, abort_reason TEXT, duration_s REAL, woke_by TEXT,
  dread INTEGER, dread_verdict TEXT, dread_reason TEXT);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, tick_id INTEGER, seq INTEGER, role TEXT, content TEXT,
  tool_name TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS messages_tick ON messages(tick_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id');
-- the per-tick context message repeats recent turns; indexing it would flood log_search
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages WHEN new.role != 'context' BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;

CREATE TABLE IF NOT EXISTS hands_runs (
  id INTEGER PRIMARY KEY, tick_id INTEGER, task TEXT, context TEXT, result_json TEXT,
  iterations INTEGER, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, duration_s REAL, ts REAL,
  tokens_cached INTEGER, transcript_json TEXT);

CREATE TABLE IF NOT EXISTS human_msgs (
  id INTEGER PRIMARY KEY, ts REAL, direction TEXT CHECK (direction IN ('in','out')),
  content TEXT, read INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS compactions (
  id INTEGER PRIMARY KEY, tick_id INTEGER, before_tokens INTEGER, after_tokens INTEGER, ts REAL,
  evicted_from INTEGER, evicted_to INTEGER, passes INTEGER, restored_sections TEXT);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY, ts REAL, tick_id INTEGER, role TEXT,
  tokens_in INTEGER, tokens_out INTEGER, tokens_cached INTEGER, cost_usd REAL, provider TEXT);
"""


def est_tokens(text: str) -> int:
    """No public GLM tokenizer; ~4 chars/token is close enough for thresholds."""
    return (len(text) + 3) // 4


def clip(text: str, cap: int, hint: str = "") -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n[... truncated {len(text) - cap} chars{hint}]"


def fts_query(q: str) -> str:
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    return " OR ".join(f'"{w}"' for w in words)


def fmt_elapsed(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m"


def fmt_span(seconds: float) -> str:
    m = int(max(seconds, 0) // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def parse_sections(text: str) -> dict[str, str]:
    """Split a summary into its fixed sections (headers like '## Current focus')."""
    out: dict[str, str] = {}
    pattern = re.compile(r"^#{1,6}\s*\**\s*(" + "|".join(re.escape(s) for s in SECTIONS) + r")\s*\**\s*:?\s*$",
                         re.I | re.M)
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        name = next(s for s in SECTIONS if s.lower() == m.group(1).lower())
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[name] = text[m.end():end].strip()
    return out


def render_sections(sec: dict[str, str]) -> str:
    return "\n\n".join(f"## {s}\n{sec.get(s, '').strip() or '(none)'}" for s in SECTIONS)


def _empty(s: str | None) -> bool:
    return not s or s.strip().lower() in ("", "(none)", "none", "-", "n/a")


@dataclass
class CompactionResult:
    evicted: list[int]
    before_tokens: int
    after_tokens: int
    passes: int
    restored: list[str]


class Memory:
    def __init__(self, cfg: Config, db_path: Path | str | None = None):
        self.cfg = cfg
        self.db_path = Path(db_path or cfg.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        if "provider" not in {r["name"] for r in self.db.execute("PRAGMA table_info(llm_calls)")}:
            self.db.execute("ALTER TABLE llm_calls ADD COLUMN provider TEXT")
        tick_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(ticks)")}
        for col, typ in (("dread", "INTEGER"), ("dread_verdict", "TEXT"), ("dread_reason", "TEXT")):
            if col not in tick_cols:
                self.db.execute(f"ALTER TABLE ticks ADD COLUMN {col} {typ}")
        if self.get("started_at") is None:
            self.set("started_at", time.time())
        self._ensure_scratch()
        self._seq: dict[int, int] = {}

    def _ensure_scratch(self) -> None:
        if self.get("scratch_note_id") is not None:
            return
        if self.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]:
            raise RuntimeError(f"{self.db_path} predates the scratch note (note {SCRATCH_ID} is taken); "
                               "start from a fresh database")
        self.db.execute("INSERT INTO notes(id, ts, title, body, tags, updated_ts) VALUES (?,?,?,?,?,?)",
                        (SCRATCH_ID, time.time(), "scratch", "", "", time.time()))
        self.set("scratch_note_id", SCRATCH_ID)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ state
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    @property
    def elapsed_s(self) -> float:
        return time.time() - float(self.get("started_at"))

    # ------------------------------------------------------------ notes
    def note_write(self, title: str, body: str, tags) -> int:
        if isinstance(tags, (list, tuple)):
            tags = ", ".join(str(t) for t in tags)
        now = time.time()
        cur = self.db.execute("INSERT INTO notes(ts, title, body, tags, updated_ts) VALUES (?,?,?,?,?)",
                              (now, title, body, tags or "", now))
        return cur.lastrowid

    def note_read(self, note_id: int) -> dict | None:
        if int(note_id) == 0:
            return {"id": 0, "title": "state summary", "body": self.get("state_summary", ""),
                    "tags": "", "ts": None, "updated_ts": self.get("state_summary_updated")}
        row = self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return dict(row) if row else None

    def note_update(self, note_id: int, body: str) -> bool:
        if int(note_id) == 0:
            raise ValueError(f"note 0 is rewritten automatically and cannot be edited; "
                             f"note {SCRATCH_ID} is your scratch note")
        cur = self.db.execute("UPDATE notes SET body=?, updated_ts=? WHERE id=?", (body, time.time(), note_id))
        return cur.rowcount > 0

    def _fts(self, sql: str, query: str, limit: int) -> list[sqlite3.Row]:
        limit = max(1, min(int(limit or 10), 50))
        rows: list = []
        try:
            rows = self.db.execute(sql, (query, limit)).fetchall()
        except sqlite3.OperationalError:
            pass  # raw query wasn't valid FTS5 syntax
        if not rows and fts_query(query):
            rows = self.db.execute(sql, (fts_query(query), limit)).fetchall()
        return rows

    def note_search(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._fts("""SELECT n.id, n.title, snippet(notes_fts, 1, '[', ']', '…', 24) AS snippet, n.ts
                            FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid
                            WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?""", query, limit)
        return [dict(r) for r in rows]

    def recent_note_titles(self, n: int = 10) -> list[tuple[int, str]]:
        # the scratch note is shown in full every turn, so it is left out of the titles list
        rows = self.db.execute("SELECT id, title FROM notes WHERE id != ? ORDER BY updated_ts DESC, id DESC LIMIT ?",
                               (SCRATCH_ID, n))
        return [(r["id"], r["title"]) for r in rows]

    # ------------------------------------------------------------ episodic log
    def new_tick(self, woke_by: str = "timer") -> int:
        cur = self.db.execute("INSERT INTO ticks(ts, elapsed_s, status, woke_by) VALUES (?,?,?,?)",
                              (time.time(), self.elapsed_s, "running", woke_by))
        return cur.lastrowid

    def finish_tick(self, tick_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE ticks SET {cols} WHERE id=?", (*fields.values(), tick_id))

    def tick(self, tick_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM ticks WHERE id=?", (tick_id,)).fetchone()
        return dict(row) if row else None

    def last_goal(self, before_tick: int) -> str | None:
        row = self.db.execute("SELECT goal FROM ticks WHERE id<? AND end_tick_called=1 ORDER BY id DESC LIMIT 1",
                              (before_tick,)).fetchone()
        return row["goal"] if row else None

    def add_message(self, tick_id: int, role: str, content: str, tool_name: str | None = None) -> None:
        seq = self._seq.get(tick_id)
        if seq is None:
            row = self.db.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE tick_id=?", (tick_id,)).fetchone()
            seq = row["s"]
        seq += 1
        self._seq = {tick_id: seq}
        self.db.execute("INSERT INTO messages(tick_id, seq, role, content, tool_name, ts) VALUES (?,?,?,?,?,?)",
                        (tick_id, seq, role, content, tool_name, time.time()))

    def record_llm_call(self, tick_id: int | None, role: str, usage) -> None:
        self.db.execute("INSERT INTO llm_calls(ts, tick_id, role, tokens_in, tokens_out, tokens_cached, cost_usd, "
                        "provider) VALUES (?,?,?,?,?,?,?,?)",
                        (time.time(), tick_id, role, usage.tokens_in, usage.tokens_out, usage.tokens_cached,
                         usage.cost_usd, usage.provider or None))

    def record_hands_run(self, tick_id: int, task: str, context: str, result: dict, usage,
                         duration_s: float, transcript: list[dict]) -> int:
        cur = self.db.execute(
            "INSERT INTO hands_runs(tick_id, task, context, result_json, iterations, tokens_in, tokens_out, "
            "tokens_cached, cost_usd, duration_s, ts, transcript_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tick_id, task, context, json.dumps(result), result.get("iterations", 0), usage.tokens_in,
             usage.tokens_out, usage.tokens_cached, usage.cost_usd, duration_s, time.time(),
             json.dumps(transcript, default=str)))
        return cur.lastrowid

    def log_search(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._fts("""SELECT m.id, m.tick_id, m.role, m.tool_name, m.ts,
                                   snippet(messages_fts, 0, '[', ']', '…', 24) AS snippet
                            FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid
                            WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?""", query, limit)
        return [dict(r) for r in rows]

    def render_turn(self, tick_id: int, item_cap: int | None = RENDER_ITEM_CAP) -> str:
        t = self.tick(tick_id)
        if not t:
            return ""
        head = f"### tick {tick_id} · {time.strftime('%Y-%m-%d %H:%M', time.gmtime(t['ts']))} UTC"
        if t["aborted"]:
            head += f" · turn aborted ({t['abort_reason']})"
        lines = [head]
        rows = self.db.execute("SELECT role, content, tool_name FROM messages WHERE tick_id=? AND role!='context' "
                               "ORDER BY seq", (tick_id,))
        for r in rows:
            c = r["content"] or ""
            if item_cap:
                c = clip(c, item_cap, "; full text via log_read")
            if r["role"] == "assistant":
                if c.strip():
                    lines.append(f"me: {c}")
            elif r["role"] == "tool_call":
                lines.append(f"→ {r['tool_name']}({c})")
            elif r["role"] == "tool":
                lines.append(f"← {r['tool_name']}: {c}")
            elif r["role"] == "reasoning":
                continue  # kept in the log for analysis, not replayed
            else:
                lines.append(f"{r['role']}: {c}")
        return "\n".join(lines)

    def log_read(self, tick_from: int, tick_to: int) -> str:
        tick_from, tick_to = int(tick_from), int(tick_to)
        if tick_to < tick_from:
            tick_from, tick_to = tick_to, tick_from
        note = ""
        if tick_to - tick_from + 1 > 20:
            tick_to = tick_from + 19
            note = f"\n[capped at 20 ticks: showing {tick_from}..{tick_to}]"
        ids = [r["id"] for r in self.db.execute("SELECT id FROM ticks WHERE id BETWEEN ? AND ? ORDER BY id",
                                                (tick_from, tick_to))]
        if not ids:
            return "no ticks in that range" + note
        return "\n\n".join(self.render_turn(i, item_cap=None) for i in ids) + note

    # ------------------------------------------------------------ human channel
    def human_send_in(self, text: str) -> int:
        return self.db.execute("INSERT INTO human_msgs(ts, direction, content, read) VALUES (?, 'in', ?, 0)",
                               (time.time(), text)).lastrowid

    def human_send_out(self, text: str) -> int:
        return self.db.execute("INSERT INTO human_msgs(ts, direction, content, read) VALUES (?, 'out', ?, 0)",
                               (time.time(), text)).lastrowid

    def unread_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM human_msgs WHERE direction='in' AND read=0").fetchone()[0]

    def max_inbound_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM human_msgs WHERE direction='in'").fetchone()[0]

    def read_inbox(self) -> list[dict]:
        rows = [dict(r) for r in self.db.execute(
            "SELECT id, ts, content FROM human_msgs WHERE direction='in' AND read=0 ORDER BY id")]
        if rows:
            self.db.execute(f"UPDATE human_msgs SET read=1 WHERE id IN ({','.join('?' * len(rows))})",
                            [r["id"] for r in rows])
        return rows

    # ------------------------------------------------------------ working context
    @property
    def scratch_cap_chars(self) -> int:
        return self.cfg.memory.scratch_max_tokens * 4

    def window(self) -> list[int]:
        """Finished ticks not yet folded into the summary, oldest first."""
        wm = int(self.get("compacted_through", 0))
        return [r["id"] for r in self.db.execute(
            "SELECT id FROM ticks WHERE id>? AND status!='running' ORDER BY id", (wm,))]

    def build_context(self, tick_id: int, budget_line: str | None = None, show_inbox: bool = True,
                      dread_line: str | None = None) -> str:
        summary = self.get("state_summary", "") or "(empty)"
        scratch = self.note_read(SCRATCH_ID)["body"] or ""
        scratch = clip(scratch, self.scratch_cap_chars, f"; full text via note_read({SCRATCH_ID})") \
            if scratch.strip() else "(empty)"
        turns = "\n\n".join(self.render_turn(i) for i in self.window()) or "(none yet)"
        notes = "\n".join(f"- [{i}] {t}" for i, t in self.recent_note_titles(10)) or "(none)"
        parts = [f"# State summary (note 0)\n{summary}",
                 f"# Scratch note (note {SCRATCH_ID})\n{scratch}",
                 f"# Recent turns\n{turns}",
                 f"# Recent notes\n{notes}"]
        if show_inbox:
            parts.append(f"# Inbox\n{self.unread_count()} unread")
        if budget_line:
            parts.append(budget_line)
        if dread_line:
            parts.append(dread_line)
        parts.append(f"[tick {tick_id} · {self.clock_line(tick_id)}]")
        return "\n\n".join(parts)

    def clock_line(self, tick_id: int) -> str:
        # P5: a bare "elapsed 2h 58m" (since the run started) was read as time since the last turn,
        # and the context had no current time to check it against
        now = time.time()
        line = f"now {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC"
        prev = self.db.execute("SELECT ts, duration_s FROM ticks WHERE id<? ORDER BY id DESC LIMIT 1",
                               (tick_id,)).fetchone()
        if prev:
            line += f" · last turn ended {fmt_span(now - prev['ts'] - (prev['duration_s'] or 0))} ago"
        else:
            line += " · first turn"
        return line + f" · running {fmt_span(self.elapsed_s)}"

    # ------------------------------------------------------------ compaction
    def should_compact(self, tick_id: int) -> bool:
        win = self.window()
        if len(win) <= 1:
            return False
        tokens = est_tokens("\n\n".join(self.render_turn(i) for i in win))
        since = tick_id - int(self.get("last_compaction_tick", 0))
        return tokens > self.cfg.memory.compact_threshold_tokens or since >= self.cfg.memory.compact_every_ticks

    def _pick_evictions(self) -> list[int]:
        win = self.window()
        keep = win[-self.cfg.memory.recent_ticks:] if self.cfg.memory.recent_ticks > 0 else []
        evict = win[: len(win) - len(keep)]
        while len(keep) > 1 and est_tokens("\n\n".join(self.render_turn(i) for i in keep)) > \
                self.cfg.memory.compact_threshold_tokens:
            evict.append(keep.pop(0))
        return evict

    async def maybe_compact(self, tick_id: int, llm: "LLM") -> CompactionResult | None:
        if not self.should_compact(tick_id):
            return None
        evict = self._pick_evictions()
        if not evict:
            return None
        return await self.compact(tick_id, llm, evict)

    async def compact(self, tick_id: int, llm: "LLM", evict: list[int]) -> CompactionResult:
        cap = self.cfg.memory.summary_max_tokens
        old = self.get("state_summary", "") or ""
        win = self.window()
        before = est_tokens(old) + est_tokens("\n\n".join(self.render_turn(i) for i in win))
        system = (PROMPTS / "compactor.md").read_text(encoding="utf-8").replace("{max_tokens}", str(cap))
        goal = self.last_goal(tick_id + 1)

        # feed evicted turns in chunks so one huge window can't blow the compactor's context
        chunks, cur, cur_tok = [], [], 0
        for i in evict:
            txt = self.render_turn(i)
            if cur and cur_tok + est_tokens(txt) > COMPACT_CHUNK_TOKENS:
                chunks.append(cur)
                cur, cur_tok = [], 0
            cur.append(txt)
            cur_tok += est_tokens(txt)
        chunks.append(cur)

        summary, passes = old, 0
        for chunk in chunks:
            user = (f"## Current summary\n{summary or '(empty — this is the first summary)'}\n\n"
                    f"## Most recent recorded focus\n{goal if goal else '(none recorded)'}\n\n"
                    f"## Turns being evicted\n" + "\n\n".join(chunk))
            summary = await self._compactor_call(llm, system, user)
            passes += 1

        if est_tokens(summary) > cap:
            summary = await self._compactor_call(
                llm, system, f"This summary is {est_tokens(summary)} tokens; the hard limit is {cap}. "
                             f"Rewrite it shorter in the same format. Keep 'Current focus' and 'Open threads' "
                             f"intact; compress the other sections.\n\n{summary}")
            passes += 1

        new_sec, old_sec = parse_sections(summary), parse_sections(old)
        restored = []
        for s in PROTECTED:
            if _empty(new_sec.get(s)) and not _empty(old_sec.get(s)):
                new_sec[s] = old_sec[s]
                restored.append(s)
        if not new_sec:  # compactor ignored the format entirely; keep its text under "What I know"
            new_sec = dict(old_sec)
            new_sec["What I know"] = (old_sec.get("What I know", "") + "\n" + summary).strip()
            restored.append("format")
        final = self._enforce_cap(new_sec, cap)

        self.set("state_summary", final)
        self.set("state_summary_updated", time.time())
        self.set("compacted_through", max(evict))
        self.set("last_compaction_tick", tick_id)
        after = est_tokens(final) + est_tokens("\n\n".join(self.render_turn(i) for i in self.window()))
        self.db.execute("INSERT INTO compactions(tick_id, before_tokens, after_tokens, ts, evicted_from, evicted_to, "
                        "passes, restored_sections) VALUES (?,?,?,?,?,?,?,?)",
                        (tick_id, before, after, time.time(), min(evict), max(evict), passes, ",".join(restored)))
        log.info("compaction at tick %d: evicted %d..%d, %d -> %d tokens, passes=%d restored=%s",
                 tick_id, min(evict), max(evict), before, after, passes, restored)
        return CompactionResult(evict, before, after, passes, restored)

    async def _compactor_call(self, llm: "LLM", system: str, user: str) -> str:
        resp = await llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              None, reasoning=self.cfg.llm.compactor_reasoning, role="compactor")
        return resp.content.strip()

    @staticmethod
    def _enforce_cap(sec: dict[str, str], cap: int) -> str:
        """Last resort after the second pass: trim unprotected sections, never focus/threads."""
        text = render_sections(sec)
        trimmable = ["What I've done", "What I know", "Notes to self"]
        while est_tokens(text) > cap:
            longest = max(trimmable, key=lambda s: len(sec.get(s, "")))
            body = sec.get(longest, "")
            if len(body) < 40:
                break
            lines = body.splitlines()
            sec[longest] = "\n".join(lines[:-1]) if len(lines) > 1 else body[: len(body) * 3 // 4]
            text = render_sections(sec)
        return text
