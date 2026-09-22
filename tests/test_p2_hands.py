"""P2: hands sub-agent, shell/file tools, return contract, caps."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent.hands import run_hands, skills_index
from agent.llm import ScriptedLLM, reply
from agent.runner import Runner
from agent.thinker import Thinker
from agent.tools import files
from agent.tools.shell import run_shell

KEYS = {"status", "summary", "artifacts", "errors", "iterations"}
PY = f'"{sys.executable}"'


def steps(messages):
    return sum(1 for m in messages if m["role"] == "assistant")


def hands(cfg, script, timeout=None, stop=lambda: False):
    return asyncio.run(run_hands("do it", "ctx", cfg=cfg, llm=ScriptedLLM(script, cfg), stop_check=stop,
                                 timeout=timeout))


def finish(**kw):
    args = {"status": "done", "summary": "said hello", "artifacts": ["/workspace/x"], "errors": []}
    args.update(kw)
    return reply("", [("finish", args)])


def test_done_contract(cfg):
    def script(messages, tools, role):
        assert role == "hands" and "do it" in messages[1]["content"]
        if steps(messages) == 0:
            return reply("Plan: echo", [("shell", {"cmd": "echo hello"})])
        return finish()

    out = hands(cfg, script)
    assert set(out.result) == KEYS
    assert out.result["status"] == "done" and out.result["iterations"] == 2
    tool_msgs = [m for m in out.transcript if m["role"] == "tool"]
    assert "hello" in json.loads(tool_msgs[0]["content"])["stdout"]
    assert out.usage.calls == 2


def test_iteration_cap(cfg):
    cfg.hands.max_iter = 3
    out = hands(cfg, lambda m, t, r: reply("still going", [("file_list", {"path": "."})]))
    assert out.result["status"] == "partial" and out.result["iterations"] == 3
    assert "step limit" in out.result["errors"][0] and out.result["summary"] == "still going"


def test_timeout(cfg):
    async def script(messages, tools, role):
        await asyncio.sleep(10)

    t0 = time.monotonic()
    out = hands(cfg, script, timeout=0.3)
    assert out.result["status"] == "timeout" and set(out.result) == KEYS
    assert time.monotonic() - t0 < 3


def test_llm_error_still_returns_contract(cfg):
    def script(messages, tools, role):
        raise RuntimeError("provider exploded")

    out = hands(cfg, script)
    assert out.result["status"] == "failed" and "provider exploded" in out.result["errors"][0]


def test_stop_checked_before_each_iteration(cfg):
    calls = {"n": 0}

    def stop():
        return calls["n"] >= 1

    def script(messages, tools, role):
        calls["n"] += 1
        return reply("", [("file_list", {"path": "."})])

    out = hands(cfg, script, stop=stop)
    assert out.result["status"] == "failed" and out.result["errors"] == ["stopped"] and calls["n"] == 1


def test_text_only_step_then_finish_and_bad_args(cfg):
    def script(messages, tools, role):
        s = steps(messages)
        if s == 0:
            return reply("thinking out loud")
        if s == 1:
            r = reply("", [("file_read", {})])
            r.tool_calls[0].arguments = "{oops"
            return r
        if s == 2:
            return reply("", [("file_read", {"nopath": 1})])
        return finish(summary=" ".join(["word"] * 500), status="bogus")

    out = hands(cfg, script)
    errors = [m["content"] for m in out.transcript if m["role"] == "tool"]
    assert "not valid JSON" in errors[0] and "missing or invalid argument" in errors[1]
    assert out.result["status"] == "partial"  # invalid status normalized
    assert len(out.result["summary"].split()) <= 301


def test_skills_index(cfg):
    sk = cfg.skills_dir
    (sk / "foo").mkdir(parents=True)
    (sk / "foo" / "SKILL.md").write_text("# Does foo things.\n\nusage: ./run.sh\n")
    (sk / "bar").mkdir()
    assert skills_index(sk) == "- foo: Does foo things."
    seen = {}

    def script(messages, tools, role):
        seen["system"] = messages[0]["content"]
        return finish()

    hands(cfg, script)
    assert "- foo: Does foo things." in seen["system"]


def test_shell_timeout_kills(tmp_path):
    t0 = time.monotonic()
    out = asyncio.run(run_shell(f'{PY} -c "import time; time.sleep(20)"', cwd=str(tmp_path), timeout=0.5))
    assert out["timed_out"] is True and time.monotonic() - t0 < 6


def test_shell_output_tail(tmp_path):
    out = asyncio.run(run_shell(f'{PY} -c "print(\'x\' * 10000 + \'END\')"', cwd=str(tmp_path), timeout=20))
    assert out["exit_code"] == 0 and out["stdout"].rstrip().endswith("END")
    assert "earlier bytes omitted" in out["stdout"] and len(out["stdout"]) < 4200


def test_shell_env_has_no_api_keys(tmp_path, monkeypatch):
    from agent.tools.shell import clean_env
    monkeypatch.setenv("LLM_API_KEY", "secret")
    env = clean_env(str(tmp_path))
    assert "LLM_API_KEY" not in env and env["HOME"] == str(tmp_path)


def test_file_tools_confined_to_workspace(cfg, tmp_path):
    ws = cfg.workspace
    assert files.file_write(ws, "a/b.txt", "hi")["path"] == "/workspace/a/b.txt"
    assert files.file_read(ws, "/workspace/a/b.txt")["content"] == "hi"
    assert "b.txt" in files.file_list(ws, "/workspace", depth=2)["entries"][1]
    (tmp_path / "data" / "state.db").write_text("secret")
    for bad in ["../data/state.db", str(tmp_path / "data" / "state.db"), "/workspace/../data/state.db"]:
        with pytest.raises(files.PathError):
            files.file_read(ws, bad)
    try:
        os.symlink(tmp_path / "data", ws / "link")
    except (OSError, NotImplementedError):
        return
    with pytest.raises(files.PathError):
        files.file_read(ws, "link/state.db")


def test_file_read_offset(cfg):
    files.file_write(cfg.workspace, "big.txt", "y" * 40000)
    first = files.file_read(cfg.workspace, "big.txt")
    assert first["truncated"] and first["next_offset"] == 32768 and len(first["content"]) == 32768
    rest = files.file_read(cfg.workspace, "big.txt", offset=first["next_offset"])
    assert not rest["truncated"] and len(rest["content"]) == 40000 - 32768


def test_thinker_dispatches_hands_through_runner(cfg):
    def script(messages, tools, role):
        if role == "hands":
            return finish(summary="found 3 files")
        if messages[-1]["role"] == "user":
            return reply("", [("hands", {"task": "count files", "context": "in /workspace"})])
        assert "found 3 files" in messages[-1]["content"]
        return reply("", [("end_tick", {"goal": "count", "status": "ok"})])

    r = Runner(cfg, ScriptedLLM(script, cfg), poll_s=0.005)
    asyncio.run(r.run(max_ticks=1))
    runs = [dict(x) for x in r.memory.db.execute("SELECT * FROM hands_runs")]
    assert len(runs) == 1 and json.loads(runs[0]["result_json"])["status"] == "done"
    assert runs[0]["cost_usd"] > 0 and runs[0]["task"] == "count files"
    t = dict(r.memory.db.execute("SELECT * FROM ticks").fetchone())
    assert t["n_hands_calls"] == 1
    roles = {x[0] for x in r.memory.db.execute("SELECT role FROM llm_calls WHERE tick_id=1")}
    assert roles == {"thinker", "hands"}


def test_hands_timeout_clamped_to_turn_time(cfg):
    from agent.memory import Memory

    seen = {}

    async def fake_hands(task, ctx, timeout):
        seen["timeout"] = timeout
        from agent.hands import HandsOutcome
        from agent.llm import Usage
        return HandsOutcome({"status": "done", "summary": "", "artifacts": [], "errors": [], "iterations": 0},
                            Usage(), [], 0.0)

    th = Thinker(cfg, Memory(cfg), None, fake_hands)
    asyncio.run(th._exec(1, "hands", {"task": "t", "context": ""}, time.monotonic() + 100))
    assert 80 <= seen["timeout"] <= 85
    asyncio.run(th._exec(1, "hands", {"task": "t", "context": ""}, time.monotonic() + 5000))
    assert seen["timeout"] == cfg.hands.timeout_s
