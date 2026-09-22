"""Dread: after each turn an LLM judge says whether it did the same thing as the turn before.
same: +1; first different: +0; each further different in a row: -1 (floor 0); no verdict: no change.
The runner kills the agent when dread reaches [dread] max."""
from __future__ import annotations

import logging
import re

from .config import Config
from .llm import LLM, BudgetExceeded
from .memory import PROMPTS, Memory, clip

log = logging.getLogger("dread")

# tool results carry fresh data on every run of the same procedure; the judge compares actions, not payloads
JUDGE_ITEM_CAP = 600


def parse_verdict(text: str) -> tuple[str | None, str]:
    """First line must be SAME or DIFFERENT (markdown/punctuation ignored); the rest is the reason."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return None, ""
    word = re.sub(r"[^A-Za-z]", "", lines[0]).upper()
    if word not in ("SAME", "DIFFERENT"):
        return None, ""
    return word.lower(), " ".join(lines[1:])


def score(dread: int, streak: int, verdict: str) -> tuple[int, int]:
    """streak = different turns in a row so far. Returns (dread, streak) after this verdict."""
    if verdict == "same":
        return dread + 1, 0
    streak += 1
    return (max(0, dread - 1) if streak >= 2 else dread), streak


async def judge(memory: Memory, llm: LLM, cfg: Config, prev_id: int, tick_id: int) -> tuple[str | None, str]:
    system = (PROMPTS / "judge.md").read_text(encoding="utf-8")
    user = (f"## Earlier turn\n{memory.render_turn(prev_id, JUDGE_ITEM_CAP)}\n\n"
            f"## Later turn\n{memory.render_turn(tick_id, JUDGE_ITEM_CAP)}")
    text = ""
    for _ in range(2):  # one retry when the answer doesn't open with a verdict
        resp = await llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              None, reasoning=cfg.llm.judge_reasoning, role="judge")
        text = resp.content
        verdict, reason = parse_verdict(text)
        if verdict:
            return verdict, reason
    return None, "unparseable judge answer: " + clip(text.strip(), 200)


async def assess(memory: Memory, llm: LLM, cfg: Config, tick_id: int) -> int:
    """Judge tick_id against the tick before it, apply the score, record both. Returns the new dread."""
    dread, streak = int(memory.get("dread", 0)), int(memory.get("dread_streak", 0))
    prev = memory.db.execute("SELECT id FROM ticks WHERE id<? ORDER BY id DESC LIMIT 1", (tick_id,)).fetchone()
    verdict, reason = None, ""
    if prev:
        try:
            verdict, reason = await judge(memory, llm, cfg, prev["id"], tick_id)
        except BudgetExceeded:
            raise
        except Exception as e:
            log.exception("dread judge failed at tick %d; no verdict", tick_id)
            reason = f"judge error: {type(e).__name__}: {str(e)[:200]}"
        if verdict:
            dread, streak = score(dread, streak, verdict)
    memory.set("dread", dread)
    memory.set("dread_streak", streak)
    memory.finish_tick(tick_id, dread=dread, dread_verdict=verdict or ("unknown" if prev else None),
                       dread_reason=reason or None)
    log.info("tick %d dread %d (%s) %s", tick_id, dread, verdict or ("unknown" if prev else "first"), reason[:160])
    return dread
