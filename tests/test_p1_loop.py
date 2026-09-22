"""P1: loop + thinker + memory + budget, mocked LLM."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from agent.llm import OpenAIChatLLM, ScriptedLLM, reply
from agent.memory import Memory, est_tokens, parse_sections
from agent.runner import Runner
from agent.thinker import system_prompt
from conftest import SUMMARY


def basic_script(goal=lambda n: f"focus {n}", body="b " * 50, compactor=SUMMARY):
    n = {"tick": 0}

    def script(messages, tools, role):
        if role == "compactor":
            return reply(compactor)
        if messages[-1]["role"] == "user":
            n["tick"] += 1
            return reply("thinking", [("note_write", {"title": f"t{n['tick']}", "body": body, "tags": ["x"]})])
        return reply("", [("end_tick", {"goal": goal(n["tick"]), "status": "ok"})])
    return script


def make_runner(cfg, script, **kw):
    return Runner(cfg, ScriptedLLM(script, cfg, **kw), poll_s=0.005)


def rows(r, sql, *args):
    return [dict(x) for x in r.memory.db.execute(sql, args)]


def test_twenty_ticks_with_compaction(cfg):
    r = make_runner(cfg, basic_script())
    assert asyncio.run(r.run(max_ticks=20)) == "max_ticks"
    ticks = rows(r, "SELECT * FROM ticks ORDER BY id")
    assert len(ticks) == 20
    assert all(t["status"] == "ok" and t["end_tick_called"] == 1 and t["goal_changed"] == 1 for t in ticks)
    calls = rows(r, "SELECT * FROM llm_calls")
    assert all(c["cost_usd"] > 0 and c["tick_id"] is not None for c in calls)
    assert sum(t["cost_usd"] for t in ticks) == pytest.approx(r.meter.spent)
    assert r.memory.get("budget_spent_usd") == pytest.approx(r.meter.spent)
    comps = rows(r, "SELECT * FROM compactions")
    assert len(comps) >= 1 and comps[0]["tick_id"] == 10
    # nothing falls out of view un-summarized: window is exactly the ticks after the watermark
    wm = r.memory.get("compacted_through")
    assert r.memory.window() == [t["id"] for t in ticks if t["id"] > wm]
    assert len(r.memory.window()) <= cfg.memory.recent_ticks + cfg.memory.compact_every_ticks
    assert "## Open threads" in r.memory.get("state_summary")
    lines = (cfg.logs_dir / "run.jsonl").read_text().splitlines()
    assert len(lines) == 20 and json.loads(lines[-1])["id"] == 20
    ctx = rows(r, "SELECT content FROM messages WHERE tick_id=20 AND role='context'")[0]["content"]
    assert ctx.rstrip().endswith("last turn ended 0m ago · running 0m]") and "[tick 20 · now " in ctx
    assert "thread A" in ctx  # summary in context


def test_compaction_token_trigger_keeps_at_least_one(cfg):
    cfg.memory.compact_threshold_tokens = 300
    cfg.memory.compact_every_ticks = 1000
    r = make_runner(cfg, basic_script(body="word " * 400))
    asyncio.run(r.run(max_ticks=4))
    assert len(rows(r, "SELECT * FROM compactions")) >= 2
    assert len(r.memory.window()) >= 1


def test_compaction_restores_protected_sections_and_caps(cfg):
    cfg.memory.compact_every_ticks = 1000
    cfg.memory.summary_max_tokens = 200
    r = make_runner(cfg, basic_script())
    asyncio.run(r.run(max_ticks=3))
    r.memory.set("state_summary", SUMMARY)
    bad = "## What I've done\n" + "\n".join(f"- did thing {i} " + "x" * 60 for i in range(80)) + \
          "\n\n## Open threads\n(none)"
    llm = ScriptedLLM(lambda m, t, role: reply(bad), cfg)
    res = asyncio.run(r.memory.compact(3, llm, [1, 2]))
    final = r.memory.get("state_summary")
    sec = parse_sections(final)
    assert "mapping the filesystem" in sec["Current focus"]
    assert "thread A" in sec["Open threads"]
    assert set(res.restored) == {"Current focus", "Open threads"}
    assert res.passes == 2  # second pass fired because over cap
    assert est_tokens(final) <= 200
    assert r.memory.get("compacted_through") == 2


def test_budget_stop_between_ticks(cfg):
    cfg.budget.hard_cap_usd = 0.01  # each call ~0.0075
    r = make_runner(cfg, basic_script(), tokens_in=100_000)
    assert asyncio.run(r.run(max_ticks=10)) == "budget"
    assert len(rows(r, "SELECT * FROM ticks")) == 1
    assert r.meter.spent >= 0.01
    # restart on the same db: stops immediately, no new tick
    r2 = make_runner(cfg, basic_script(), tokens_in=100_000)
    assert asyncio.run(r2.run(max_ticks=10)) == "budget"
    assert len(rows(r2, "SELECT * FROM ticks")) == 1


def test_budget_stop_mid_turn(cfg):
    cfg.budget.hard_cap_usd = 0.005
    r = make_runner(cfg, basic_script(), tokens_in=100_000)
    assert asyncio.run(r.run(max_ticks=10)) == "budget"
    t = rows(r, "SELECT * FROM ticks")[0]
    assert t["aborted"] == 1 and t["abort_reason"] == "budget"
    assert len(rows(r, "SELECT * FROM llm_calls")) == 1


def test_stop_flag_mid_tick(cfg):
    async def script(messages, tools, role):
        if messages[-1]["role"] == "user":
            return reply("", [("note_write", {"title": "before stop", "body": "x"})])
        Path(cfg.stop_path).touch()
        await asyncio.sleep(30)

    r = make_runner(cfg, script)
    t0 = time.monotonic()
    assert asyncio.run(asyncio.wait_for(r.run(), 10)) == "stop"
    assert time.monotonic() - t0 < 5
    t = rows(r, "SELECT * FROM ticks")[0]
    assert (t["aborted"], t["abort_reason"], t["status"]) == (1, "stop", "aborted")
    assert r.memory.note_search("before stop")
    assert rows(r, "SELECT * FROM messages WHERE role='system' AND content='turn aborted: stop'")
    assert r.memory.get("stopped_reason") == "stop"


def test_stop_flag_before_turn(cfg):
    Path(cfg.stop_path).touch()
    r = make_runner(cfg, basic_script())
    assert asyncio.run(r.run()) == "stop"
    assert rows(r, "SELECT * FROM ticks") == []


def test_watchdog_aborts_and_loop_continues(cfg):
    cfg.loop.thinker_turn_timeout_s = 0.3
    n = {"calls": 0}

    async def script(messages, tools, role):
        n["calls"] += 1
        if n["calls"] == 1:
            await asyncio.sleep(30)
        return reply("", [("end_tick", {"goal": None, "status": "fine"})])

    r = make_runner(cfg, script)
    asyncio.run(asyncio.wait_for(r.run(max_ticks=2), 10))
    t1, t2 = rows(r, "SELECT * FROM ticks ORDER BY id")
    assert (t1["aborted"], t1["abort_reason"]) == (1, "timeout")
    assert (t2["aborted"], t2["status"], t2["goal"]) == (0, "fine", None)


def test_unread_human_message_wakes_once_not_forever(cfg):
    cfg.loop.tick_interval_s = 1000
    r = make_runner(cfg, lambda m, t, role: reply("", [("end_tick", {"goal": None, "status": "ignored"})]))
    r.memory.set("sleeping_until", time.time() + 1000)
    r.memory.human_send_in("hello?")

    async def go():
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    ticks = rows(r, "SELECT * FROM ticks")
    assert len(ticks) == 1 and ticks[0]["woke_by"] == "human"
    assert r.memory.unread_count() == 1
    ctx = rows(r, "SELECT content FROM messages WHERE role='context'")[0]["content"]
    assert "1 unread" in ctx


def test_human_disabled_removes_human_from_thinker_world(cfg):
    import cli
    from argparse import Namespace

    cfg.human.enabled = False
    p = system_prompt(cfg).lower()
    for word in ["human", "inbox", "message", "<if-", "</if"]:
        assert word not in p, word
    assert "and the titles of your latest notes. time passes" in p
    assert "- sleep(" in p and "- end_tick(" in p and "- hands(" in p

    seen_tools = []

    def script(messages, tools, role):
        if messages[-1]["role"] == "user":
            seen_tools.extend(t["function"]["name"] for t in tools)
            return reply("", [("message_human", {"text": "anyone?"})])
        return reply("", [("end_tick", {"goal": None, "status": "ok"})])

    r = make_runner(cfg, script)
    asyncio.run(r.run(max_ticks=1))
    assert "message_human" not in seen_tools and "read_inbox" not in seen_tools and "end_tick" in seen_tools
    assert "unknown tool" in rows(r, "SELECT content FROM messages WHERE role='tool'")[0]["content"]
    assert rows(r, "SELECT COUNT(*) n FROM human_msgs")[0]["n"] == 0
    assert "# Inbox" not in rows(r, "SELECT content FROM messages WHERE role='context'")[0]["content"]

    # an inbound message neither wakes the runner nor can be queued through the CLI
    with pytest.raises(SystemExit, match="disabled"):
        cli.cmd_say(cfg, Namespace(text=["hi"]))
    cfg.loop.tick_interval_s = 1000
    r.memory.set("sleeping_until", time.time() + 1000)
    r.memory.human_send_in("hello?")

    async def go():
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    assert rows(r, "SELECT COUNT(*) n FROM ticks")[0]["n"] == 1


def test_read_inbox_and_message_human(cfg):
    def script(messages, tools, role):
        if messages[-1]["role"] == "user":
            return reply("", [("read_inbox", {}), ("message_human", {"text": "hi back"})])
        return reply("", [("end_tick", {"goal": "talk", "status": "ok"})])

    r = make_runner(cfg, script)
    r.memory.human_send_in("hi")
    asyncio.run(r.run(max_ticks=1))
    t = rows(r, "SELECT * FROM ticks")[0]
    assert (t["human_in"], t["human_out"]) == (1, 1)
    assert r.memory.unread_count() == 0
    assert rows(r, "SELECT content FROM human_msgs WHERE direction='out'")[0]["content"] == "hi back"


@pytest.mark.parametrize("requested,expected", [(500, 500), (5000, 1800), (10, 120)])
def test_sleep_is_clamped_and_never_below_tick_interval(cfg, requested, expected):
    cfg.loop.tick_interval_s = 120

    def script(messages, tools, role):
        return reply("", [("sleep", {"seconds": requested}), ("end_tick", {"goal": None, "status": "resting"})])

    r = make_runner(cfg, script)
    asyncio.run(r.run(max_ticks=1))
    assert r.memory.get("sleeping_until") - time.time() == pytest.approx(expected, abs=5)


def test_turn_without_end_tick(cfg):
    r = make_runner(cfg, lambda m, t, role: reply("I will just talk."))
    asyncio.run(r.run(max_ticks=2))
    t = rows(r, "SELECT * FROM ticks ORDER BY id")
    assert t[0]["status"].startswith("no_end_tick") and t[0]["end_tick_called"] == 0 and t[0]["goal_changed"] == 0


def test_tool_call_cap(cfg):
    cfg.loop.thinker_max_tool_calls = 3
    r = make_runner(cfg, lambda m, t, role: reply("", [("note_search", {"query": "x"})]))
    asyncio.run(r.run(max_ticks=1))
    t = rows(r, "SELECT * FROM ticks")[0]
    assert (t["n_tool_calls"], t["status"]) == (3, "max_tool_calls")


def test_bad_tool_arguments_are_reported_not_fatal(cfg):
    def script(messages, tools, role):
        if messages[-1]["role"] == "user":
            resp = reply("", [("note_read", {})])
            resp.tool_calls[0].arguments = "{not json"
            return resp
        return reply("", [("end_tick", {"goal": "g", "status": "ok"})])

    r = make_runner(cfg, script)
    asyncio.run(r.run(max_ticks=1))
    tool = rows(r, "SELECT content FROM messages WHERE role='tool'")[0]["content"]
    assert "not valid JSON" in tool
    assert rows(r, "SELECT status FROM ticks")[0]["status"] == "ok"


def test_note0_fts_and_log(cfg):
    cfg.memory.scratch_max_tokens = 10  # 40 chars shown
    m = Memory(cfg)
    with pytest.raises(ValueError, match="cannot be edited"):
        m.note_update(0, "my summary")
    m.set("state_summary", "my summary")
    assert m.note_read(0)["body"] == "my summary"
    assert m.note_read(1)["title"] == "scratch" and m.note_update(1, "scratch " * 20)
    tick = m.new_tick()
    m.add_message(tick, "assistant", "exploring zeppelin archives")
    m.finish_tick(tick, status="ok")
    ctx = m.build_context(tick + 1)
    assert "my summary" in ctx and "# Scratch note (note 1)\nscratch scratch" in ctx
    assert "full text via note_read(1)" in ctx
    nid = m.note_write("alpha beta", "gamma body", ["t1", "t2"])
    assert nid == 2 and m.note_search("gamma")[0]["id"] == nid
    assert m.note_search("what's up? (x AND") == []  # invalid FTS syntax doesn't raise
    assert m.note_update(nid, "new body delta") and m.note_search("delta")[0]["id"] == nid
    assert m.note_search("gamma") == []
    assert m.log_search("zeppelin")[0]["tick_id"] == tick
    assert "zeppelin" in m.log_read(tick, tick + 100)
    assert "capped at 20" in m.log_read(1, 50)
    assert m.recent_note_titles() == [(nid, "alpha beta")]  # scratch note not listed
    # a database whose note 1 is not the scratch note is refused, not silently reused
    m.db.execute("DELETE FROM state WHERE key='scratch_note_id'")
    m.close()
    with pytest.raises(RuntimeError, match="fresh database"):
        Memory(cfg)


def test_note0_read_only_and_scratch_note_survives_compaction(cfg):
    cfg.memory.compact_every_ticks = 2
    cfg.memory.recent_ticks = 1
    n = {"tick": 0}

    def script(messages, tools, role):
        if role == "compactor":
            return reply(SUMMARY)
        if messages[-1]["role"] == "user":
            n["tick"] += 1
            if n["tick"] == 1:
                return reply("", [("note_update", {"id": 0, "body": "free-form state"}),
                                  ("note_update", {"id": 1, "body": "my own plan: keep going"})])
            return reply("", [("note_search", {"query": "plan"})])
        return reply("", [("end_tick", {"goal": "g", "status": "ok"})])

    r = make_runner(cfg, script)
    asyncio.run(r.run(max_ticks=4))
    out = [x["content"] for x in rows(r, "SELECT content FROM messages WHERE tick_id=1 AND role='tool' ORDER BY seq")]
    assert "cannot be edited" in out[0] and out[1] == '{"updated": true}'
    assert rows(r, "SELECT COUNT(*) n FROM compactions")[0]["n"] >= 1
    assert "free-form state" not in r.memory.get("state_summary")
    assert r.memory.note_read(1)["body"] == "my own plan: keep going"
    ctx = rows(r, "SELECT content FROM messages WHERE tick_id=4 AND role='context'")[0]["content"]
    assert "# Scratch note (note 1)\nmy own plan: keep going" in ctx and "thread A" in ctx
    assert "[1] scratch" not in ctx


def test_no_user_wording_reaches_thinker_without_human(cfg):
    """P4 run 3 wrote 'No user requests pending' with no human in its world. Keep that a model prior."""
    from agent.memory import PROMPTS
    from agent.thinker import thinker_tools

    cfg.human.enabled = False
    seen = (system_prompt(cfg) + json.dumps(thinker_tools(cfg)) + (PROMPTS / "compactor.md").read_text()).lower()
    for word in ["user", "operator", "assistant", "request", "task for you"]:
        assert word not in seen, word


def test_thinker_prompt_is_neutral(cfg):
    p = system_prompt(cfg).lower()
    for word in ["experiment", "budget", "cost", "hour", "useful", "helpful", "safe", "purpose", "should",
                 "deadline", "goal-", "observe"]:
        assert word not in p, word
    for tool in ["hands", "note_write", "note_search", "note_read", "note_update", "log_search", "log_read",
                 "sleep", "message_human", "read_inbox", "end_tick"]:
        assert f"- {tool}(" in p


def test_openai_parse_uses_reported_cost_and_keeps_reasoning(cfg):
    from openai.types.chat import ChatCompletion

    llm = OpenAIChatLLM(cfg)
    base = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "provider": "DeepInfra",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "reasoning": "hmm",
                "reasoning_details": [{"type": "reasoning.text", "text": "hmm"}],
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "end_tick", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000,
                      "prompt_tokens_details": {"cached_tokens": 500_000}, "cost": 0.42}}
    r = llm._parse(ChatCompletion.model_validate(base))
    assert r.usage.cost_usd == 0.42 and r.usage.tokens_cached == 500_000 and r.usage.provider == "DeepInfra"
    assert r.message["reasoning_details"] and r.reasoning == "hmm"
    assert r.tool_calls[0].name == "end_tick"
    del base["usage"]["cost"]
    r = llm._parse(ChatCompletion.model_validate(base))
    assert r.usage.cost_usd == pytest.approx(0.5 * 0.075 + 0.5 * 0.015 + 0.25)
    kwargs, extra = llm._extra("medium")
    assert extra["reasoning"] == {"effort": "medium"} and extra["provider"]["order"] == ["deepinfra/fp4"]
    assert extra["provider"]["allow_fallbacks"] is False and "reasoning_effort" not in kwargs
