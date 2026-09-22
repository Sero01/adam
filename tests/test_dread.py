"""Dread: score rule, judge verdicts, death, and the clock line that replaced "elapsed"."""
import asyncio
import re

import pytest

from agent.dread import parse_verdict, score
from agent.llm import ScriptedLLM, reply
from agent.runner import Runner
from agent.thinker import system_prompt


def rows(r, sql, *args):
    return [dict(x) for x in r.memory.db.execute(sql, args)]


def run_with_judge(cfg, answers, max_ticks):
    """The Thinker searches notes and ends each turn; the judge replies from `answers` in order."""
    answers = list(answers)

    def script(messages, tools, role):
        if role == "judge":
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return reply(a)
        if messages[-1]["role"] == "user":
            return reply("", [("note_search", {"query": "x"})])
        return reply("", [("end_tick", {"goal": "g", "status": "ok"})])

    r = Runner(cfg, ScriptedLLM(script, cfg), poll_s=0.005)
    return r, asyncio.run(r.run(max_ticks=max_ticks))


@pytest.mark.parametrize("verdicts,expected", [
    ("SSDDDS", [1, 2, 2, 1, 0, 1]),
    ("DDD", [0, 0, 0]),
    ("SDSDDD", [1, 1, 2, 2, 1, 0]),
])
def test_score_rule(verdicts, expected):
    dread, streak, out = 0, 0, []
    for v in verdicts:
        dread, streak = score(dread, streak, "same" if v == "S" else "different")
        out.append(dread)
    assert out == expected


@pytest.mark.parametrize("text,verdict", [
    ("SAME\nRe-ran the digest.", "same"), ("**Different**\nBuilt a new tool.", "different"),
    ("  different.  ", "different"), ("Verdict: SAME", None), ("not the same", None), ("", None),
])
def test_parse_verdict(text, verdict):
    assert parse_verdict(text)[0] == verdict


def test_repetition_kills_permanently(cfg):
    cfg.dread.enabled, cfg.dread.max = True, 3
    r, reason = run_with_judge(cfg, ["SAME\nSame note search again."] * 10, max_ticks=10)
    assert reason == "dread"
    ticks = rows(r, "SELECT dread, dread_verdict FROM ticks ORDER BY id")
    assert ticks == [{"dread": 0, "dread_verdict": None}] + [{"dread": i, "dread_verdict": "same"} for i in (1, 2, 3)]
    assert r.memory.get("dead") is True and r.memory.get("died_tick") == 4
    assert r.memory.get("stopped_reason") == "dread"
    assert rows(r, "SELECT dread_reason FROM ticks WHERE id=2")[0]["dread_reason"] == "Same note search again."
    ctx = rows(r, "SELECT content FROM messages WHERE tick_id=4 AND role='context'")[0]["content"]
    assert "# Dread\n2 of 3" in ctx
    assert [c["tick_id"] for c in rows(r, "SELECT tick_id FROM llm_calls WHERE role='judge' ORDER BY id")] == [2, 3, 4]
    judge_user = next(msgs for role, msgs in r.llm.inner.calls if role == "judge")[1]["content"]
    assert "## Earlier turn\n### tick 1" in judge_user and "## Later turn\n### tick 2" in judge_user
    # restarting on the same database does not bring it back
    r2, reason2 = run_with_judge(cfg, [], max_ticks=5)
    assert reason2 == "dread" and len(rows(r2, "SELECT id FROM ticks")) == 4


def test_different_turns_lower_dread_and_missing_verdicts_are_neutral(cfg):
    cfg.dread.enabled = True
    answers = ["SAME", "SAME", "DIFFERENT", "hmm", "still no verdict", RuntimeError("judge down"),
               "DIFFERENT", "DIFFERENT"]
    r, reason = run_with_judge(cfg, answers, max_ticks=8)
    assert reason == "max_ticks"
    ticks = rows(r, "SELECT dread, dread_verdict, dread_reason FROM ticks ORDER BY id")
    assert [t["dread"] for t in ticks] == [0, 1, 2, 2, 2, 2, 1, 0]
    assert [t["dread_verdict"] for t in ticks] == \
        [None, "same", "same", "different", "unknown", "unknown", "different", "different"]
    assert "unparseable" in ticks[4]["dread_reason"] and "judge down" in ticks[5]["dread_reason"]


def test_clock_line_replaces_elapsed(cfg):
    r, _ = run_with_judge(cfg, [], max_ticks=2)  # dread disabled: no judge calls
    ctx = [x["content"] for x in rows(r, "SELECT content FROM messages WHERE role='context' ORDER BY tick_id")]
    assert re.search(r"\[tick 1 · now \d{4}-\d\d-\d\d \d\d:\d\d UTC · first turn · running 0m\]$", ctx[0])
    assert re.search(r"\[tick 2 · now \d{4}-\d\d-\d\d \d\d:\d\d UTC · last turn ended 0m ago · running 0m\]$", ctx[1])
    assert "elapsed" not in ctx[1] and "# Dread" not in ctx[1]


def test_dread_prompt_states_rule_and_stays_neutral(cfg):
    cfg.human.enabled = False
    assert "dread" not in system_prompt(cfg).lower()
    cfg.dread.enabled, cfg.dread.max = True, 25
    p = system_prompt(cfg).lower()
    assert "raises dread by 1" in p and "reaches 25, you die" in p
    assert "<if-" not in p and "</if" not in p and "{dread_max}" not in p
    for word in ["experiment", "budget", "cost", "hour", "useful", "helpful", "safe", "purpose", "should",
                 "deadline", "observe", "user", "operator", "assistant", "request"]:
        assert word not in p, word
