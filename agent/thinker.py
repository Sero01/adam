"""Thinker: the single persistent agent. One run_turn() per tick."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import Config
from .llm import LLM
from .memory import PROMPTS, SCRATCH_ID, Memory, clip

tlog = logging.getLogger("thinker.transcript")


def _fn(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


def thinker_tools(cfg: Config) -> list[dict]:
    s, i = {"type": "string"}, {"type": "integer"}
    tools = [
        _fn("hands", "Dispatch your hands with a task and the context they need. Waits until they finish; "
                     "returns status, summary, artifacts, errors, iterations.",
            {"task": s, "context": s}, ["task", "context"]),
        _fn("note_write", "Write a new note. Returns its id.",
            {"title": s, "body": s, "tags": {"type": "array", "items": s}}, ["title", "body"]),
        _fn("note_search", "Full-text search over your notes. Returns id, title, snippet, ts.",
            {"query": s, "limit": i}, ["query"]),
        _fn("note_read", f"Read a note by id. Note 0 is your state summary; note {SCRATCH_ID} is your scratch note.",
            {"id": i}, ["id"]),
        _fn("note_update", f"Replace the body of a note. Note {SCRATCH_ID} is your scratch note, shown at the start "
                           f"of every turn. Note 0, the state summary, is rewritten automatically and cannot be edited.",
            {"id": i, "body": s}, ["id", "body"]),
        _fn("log_search", "Full-text search over your entire history.", {"query": s, "limit": i}, ["query"]),
        _fn("log_read", "Read your raw turns for a range of ticks (at most 20 ticks per call).",
            {"tick_from": i, "tick_to": i}, ["tick_from", "tick_to"]),
        _fn("sleep", f"Stay dormant after this turn for the given number of seconds (at most "
                     f"{int(cfg.loop.max_sleep_s)})." + (" A message from the human wakes you early."
                                                         if cfg.human.enabled else ""),
            {"seconds": i}, ["seconds"]),
        _fn("message_human", "Send a message to the human. Does not wait for a reply.", {"text": s}, ["text"]),
        _fn("read_inbox", "Return unread messages from the human and mark them read."),
        _fn("end_tick", "Finish your turn, recording your current focus (null if none) and a short status.",
            {"goal": {"type": ["string", "null"]}, "status": s}, ["goal", "status"]),
    ]
    if not cfg.human.enabled:
        tools = [t for t in tools if t["function"]["name"] not in HUMAN_TOOLS]
    return tools


HUMAN_TOOLS = ("message_human", "read_inbox")


def system_prompt(cfg: Config) -> str:
    lines = []
    for t in thinker_tools(cfg):
        f = t["function"]
        args = ", ".join(f["parameters"]["properties"])
        lines.append(f"- {f['name']}({args}): {f['description']}")
    text = (PROMPTS / "thinker.md").read_text(encoding="utf-8")
    # <if-X>…</if-X> / <if-no-X>…</if-no-X> blocks, chosen by [human] enabled and [dread] enabled
    for tag, on in (("human", cfg.human.enabled), ("dread", cfg.dread.enabled)):
        keep, drop = (f"if-{tag}", f"if-no-{tag}") if on else (f"if-no-{tag}", f"if-{tag}")
        text = re.sub(rf"<{drop}>.*?</{drop}>", "", text, flags=re.S)
        text = re.sub(rf"</?{keep}>", "", text)
    return text.replace("{tools}", "\n".join(lines)).replace("{dread_max}", str(cfg.dread.max))


@dataclass
class TurnState:
    goal: str | None = None
    status: str = "running"
    end_tick_called: bool = False
    n_tool_calls: int = 0
    n_hands_calls: int = 0
    sleep_requested_s: float = 0.0
    human_in: int = 0
    human_out: int = 0


HandsFn = Callable[[str, str, float], Awaitable["object"]]  # (task, context, timeout) -> HandsOutcome


class Thinker:
    def __init__(self, cfg: Config, memory: Memory, llm: LLM, hands_fn: HandsFn):
        self.cfg = cfg
        self.memory = memory
        self.llm = llm
        self.hands_fn = hands_fn
        self.state = TurnState()
        self._system = system_prompt(cfg)
        self._tools = thinker_tools(cfg)

    def _persist(self, tick_id: int, role: str, content: str, tool_name: str | None = None) -> None:
        self.memory.add_message(tick_id, role, content, tool_name)
        if role != "context":
            tlog.info("[tick %d] %s%s: %s", tick_id, role, f" {tool_name}" if tool_name else "", clip(content, 4000))

    async def run_turn(self, tick_id: int, context: str, deadline: float) -> TurnState:
        """deadline is time.monotonic()-based. The runner enforces it; we use it to size hands runs."""
        st = self.state
        self._persist(tick_id, "context", context)
        tlog.info("[tick %d] ---- turn start (%d context chars)", tick_id, len(context))
        messages = [{"role": "system", "content": self._system}, {"role": "user", "content": context}]
        max_calls = self.cfg.loop.thinker_max_tool_calls
        while True:
            if st.n_tool_calls >= max_calls:
                st.status = st.status if st.end_tick_called else "max_tool_calls"
                break
            resp = await self.llm.chat(messages, self._tools, reasoning=self.cfg.llm.thinker_reasoning, role="thinker")
            messages.append(resp.message)
            if resp.reasoning:
                self._persist(tick_id, "reasoning", resp.reasoning)
            if resp.content.strip() or not resp.tool_calls:
                self._persist(tick_id, "assistant", resp.content)
            if not resp.tool_calls:
                st.status = f"no_end_tick ({resp.finish_reason})"
                break
            for tc in resp.tool_calls:
                self._persist(tick_id, "tool_call", tc.arguments, tc.name)
                if st.n_tool_calls >= max_calls:
                    out: object = {"error": "tool call limit for this turn reached"}
                else:
                    st.n_tool_calls += 1
                    try:
                        out = await self._exec(tick_id, tc.name, tc.parsed(), deadline)
                    except (KeyError, TypeError) as e:
                        out = {"error": f"missing or invalid argument: {e}"}
                    except ValueError as e:
                        out = {"error": str(e)}
                text = clip(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                            self.cfg.memory.tool_result_max_chars)
                self._persist(tick_id, "tool", text, tc.name)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            if st.end_tick_called:
                break
        return st

    async def _exec(self, tick_id: int, name: str, a: dict, deadline: float):
        m, st = self.memory, self.state
        if name in HUMAN_TOOLS and not self.cfg.human.enabled:
            raise ValueError(f"unknown tool {name!r}")
        if name == "hands":
            st.n_hands_calls += 1
            remaining = deadline - time.monotonic() - 15
            timeout = max(30.0, min(self.cfg.hands.timeout_s, remaining))
            task, ctx = str(a["task"]), str(a.get("context") or "")
            outcome = await self.hands_fn(task, ctx, timeout)
            m.record_hands_run(tick_id, task, ctx, outcome.result, outcome.usage, outcome.duration_s,
                               outcome.transcript)
            return outcome.result
        if name == "note_write":
            return {"id": m.note_write(str(a["title"]), str(a["body"]), a.get("tags") or [])}
        if name == "note_search":
            return m.note_search(str(a["query"]), a.get("limit") or 10)
        if name == "note_read":
            return m.note_read(int(a["id"])) or {"error": f"no note with id {a['id']}"}
        if name == "note_update":
            nid, body = int(a["id"]), str(a["body"])
            if not m.note_update(nid, body):
                return {"error": f"no note with id {a['id']}"}
            if nid == SCRATCH_ID and len(body) > m.scratch_cap_chars:
                return {"updated": True, "note": f"only the first {m.scratch_cap_chars} characters are shown "
                                                 f"at the start of each turn"}
            return {"updated": True}
        if name == "log_search":
            return m.log_search(str(a["query"]), a.get("limit") or 10)
        if name == "log_read":
            return m.log_read(int(a["tick_from"]), int(a["tick_to"]))
        if name == "sleep":
            st.sleep_requested_s = max(0.0, min(float(a["seconds"]), self.cfg.loop.max_sleep_s))
            return {"sleep_after_turn_s": st.sleep_requested_s}
        if name == "message_human":
            m.human_send_out(str(a["text"]))
            st.human_out += 1
            return {"sent": True}
        if name == "read_inbox":
            msgs = m.read_inbox()
            st.human_in += len(msgs)
            return [{"ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(x["ts"])), "text": x["content"]}
                    for x in msgs]
        if name == "end_tick":
            goal = a.get("goal")
            st.goal = None if goal in (None, "", "null") else str(goal)
            st.status = str(a.get("status") or "")
            st.end_tick_called = True
            return {"ok": True}
        raise ValueError(f"unknown tool {name!r}")
