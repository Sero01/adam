"""Real openai SDK path over a mock HTTP transport: request shape, reasoning passthrough, retries."""
import asyncio
import json
import time
import types

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError

import agent.llm as llm_mod
from agent.llm import OpenAIChatLLM, ScriptedLLM, reply
from agent.runner import Runner


def completion(model, msg, cost=0.001):
    return {"id": "x", "object": "chat.completion", "created": 0, "model": model, "provider": "StreamLake",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": msg}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                      "prompt_tokens_details": {"cached_tokens": 40}, "cost": cost}}


def with_transport(cfg, handler):
    llm = OpenAIChatLLM(cfg)
    llm.client = AsyncOpenAI(api_key="k", base_url=cfg.llm.base_url, max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return llm


def test_request_shape_and_reasoning_details_roundtrip(cfg):
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            msg = {"role": "assistant", "content": None, "reasoning": "let me look",
                   "reasoning_details": [{"type": "reasoning.text", "text": "let me look"}],
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "note_search", "arguments": '{"query": "x"}'}}]}
        else:
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c2", "type": "function",
                                   "function": {"name": "end_tick", "arguments": '{"goal": "g", "status": "ok"}'}}]}
        return httpx.Response(200, json=completion(body["model"], msg))

    r = Runner(cfg, with_transport(cfg, handler), poll_s=0.005)
    asyncio.run(r.run(max_ticks=1))
    first = bodies[0]
    assert first["model"] == "z-ai/glm-5.3-flash" and first["max_tokens"] == 4096
    assert first["reasoning"] == {"effort": "medium"} and "reasoning_effort" not in first
    assert first["provider"] == {"order": ["deepinfra/fp4"], "allow_fallbacks": False, "require_parameters": True}
    assert {t["function"]["name"] for t in first["tools"]} >= {"hands", "end_tick", "sleep"}
    assistant = [m for m in bodies[1]["messages"] if m["role"] == "assistant"][0]
    assert assistant["reasoning_details"][0]["text"] == "let me look"
    assert bodies[1]["messages"][-1]["role"] == "tool" and bodies[1]["messages"][-1]["tool_call_id"] == "c1"
    t = dict(r.memory.db.execute("SELECT * FROM ticks").fetchone())
    assert t["cost_usd"] == pytest.approx(0.002) and t["tokens_cached"] == 80 and t["goal"] == "g"
    assert [x[0] for x in r.memory.db.execute("SELECT provider FROM llm_calls")] == ["StreamLake", "StreamLake"]
    assert r.memory.db.execute("SELECT content FROM messages WHERE role='reasoning'").fetchone()[0] == "let me look"


def test_retries_transient_then_raises_on_bad_request(cfg, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(llm_mod, "asyncio", types.SimpleNamespace(sleep=no_sleep, iscoroutine=asyncio.iscoroutine))
    n = {"calls": 0}

    def flaky(req):
        n["calls"] += 1
        if n["calls"] < 3:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json=completion("m", {"role": "assistant", "content": "hi"}))

    resp = asyncio.run(with_transport(cfg, flaky).chat([{"role": "user", "content": "x"}], None,
                                                       reasoning="low", role="hands"))
    assert resp.content == "hi" and n["calls"] == 3

    def bad(req):
        return httpx.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(BadRequestError):
        asyncio.run(with_transport(cfg, bad).chat([{"role": "user", "content": "x"}], None,
                                                  reasoning="low", role="hands"))


def test_max_runtime_stops_between_ticks(cfg):
    cfg.loop.max_runtime_s = 0.3
    cfg.loop.tick_interval_s = 0.1
    r = Runner(cfg, ScriptedLLM(lambda m, t, role: reply("", [("end_tick", {"goal": None, "status": "ok"})]), cfg),
               poll_s=0.01)
    t0 = time.monotonic()
    assert asyncio.run(asyncio.wait_for(r.run(), 10)) == "max_runtime"
    assert time.monotonic() - t0 < 2
    assert r.memory.get("stopped_reason") == "max_runtime"
    assert time.time() - r.memory.get("heartbeat_at") < 5
    assert "max_runtime" not in r.memory.db.execute("SELECT group_concat(content) FROM messages").fetchone()[0]
