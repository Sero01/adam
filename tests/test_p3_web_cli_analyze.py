"""P3: web tools, human CLI, analyze.py, config file."""
import asyncio
import time
from pathlib import Path

import httpx
import pytest

import analyze
import cli
from agent.config import load_config
from agent.llm import ScriptedLLM, reply
from agent.memory import Memory
from agent.runner import Runner
from agent.tools import web
from conftest import ROOT, SUMMARY

DDG = """
<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Example <b>A</b></a>
<a class="result__snippet" href="#">Snippet &amp; one</a></div>
<div class="result"><a rel="nofollow" class="result__a" href="https://example.org/b">B</a>
<a class="result__snippet" href="#">two</a></div>
"""


def test_ddg_parse():
    items = web._parse_ddg(DDG)
    assert items == [{"title": "Example A", "url": "https://example.com/a", "snippet": "Snippet & one"},
                     {"title": "B", "url": "https://example.org/b", "snippet": "two"}]


def test_html_to_markdown():
    md = web.html_to_markdown("<html><script>evil()</script><h1>Title</h1><p>Hello <a href='/x'>link</a></p></html>")
    assert "evil" not in md and "# Title" in md and "[link](/x)" in md


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_web_fetch_html_truncation_and_binary():
    def handler(req):
        if req.url.path == "/bin":
            return httpx.Response(200, content=b"\x00\x01", headers={"content-type": "image/png"})
        return httpx.Response(200, text="<html><body><p>" + "z" * 20000 + "</p></body></html>",
                              headers={"content-type": "text/html; charset=utf-8"})

    async def go():
        async with _client(handler) as c:
            page = await web.web_fetch("https://site.test/page", client=c)
            binary = await web.web_fetch("https://site.test/bin", client=c)
        return page, binary

    page, binary = asyncio.run(go())
    assert page["truncated"] and len(page["content"]) == web.FETCH_CAP
    assert "non-text content" in binary["content"]
    with pytest.raises(web.WebError):
        asyncio.run(web.web_fetch("file:///etc/passwd"))


def test_tavily_search_and_missing_key():
    def handler(req):
        assert req.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"results": [{"title": f"t{i}", "url": f"u{i}", "content": "c"}
                                                     for i in range(12)]})

    async def go():
        async with _client(handler) as c:
            return await web.web_search("q", provider="tavily", api_key="k", client=c)

    items = asyncio.run(go())
    assert len(items) == 8 and items[0] == {"title": "t0", "url": "u0", "snippet": "c"}
    with pytest.raises(web.WebError):
        asyncio.run(web.web_search("q", provider="tavily", api_key=""))


def test_repo_config_loads():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.llm.model == "z-ai/glm-5.3-flash"
    assert cfg.loop.thinker_turn_timeout_s > cfg.hands.timeout_s
    assert cfg.llm.provider.allow_fallbacks is False
    # the OpenRouter key is limited to $3: the cap must stop the run before the key does
    assert cfg.budget.soft_warn_usd < cfg.budget.hard_cap_usd < 3.0


def write_cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'[paths]\nworkspace = "{(tmp_path / "ws").as_posix()}"\n'
                 f'data_dir = "{(tmp_path / "data").as_posix()}"\n', encoding="utf-8")
    return str(p)


def test_cli_commands(cfg, tmp_path, capsys):
    path = write_cfg(tmp_path)
    m = Memory(load_config(path))
    tick = m.new_tick()
    m.finish_tick(tick, status="ok", goal="read the news", end_tick_called=1, cost_usd=0.001)
    m.set("current_goal", "read the news")
    m.human_send_out("hello human")
    m.set("sleeping_until", time.time() + 60)

    cli.main(["--config", path, "say", "hello", "agent"])
    assert m.read_inbox()[0]["content"] == "hello agent"
    cli.main(["--config", path, "status"])
    out = capsys.readouterr().out
    assert "read the news" in out and "sleeping" in out and "heartbeat:   none recorded" in out
    m.set("heartbeat_at", time.time() - 10)
    cli.main(["--config", path, "status"])
    assert "heartbeat:   10s ago" in (out := capsys.readouterr().out) and "STALE" not in out
    m.set("heartbeat_at", time.time() - 3600)
    cli.main(["--config", path, "status"])
    assert "STALE" in capsys.readouterr().out
    cli.main(["--config", path, "inbox"])
    assert "hello human" in capsys.readouterr().out
    cli.main(["--config", path, "inbox"])
    assert "no new messages" in capsys.readouterr().out
    cli.main(["--config", path, "tail", "5"])
    assert "read the news" in capsys.readouterr().out
    cli.main(["--config", path, "stop"])
    assert (tmp_path / "data" / "STOP").exists()


def test_analyze_on_real_run(cfg, tmp_path):
    n = {"tick": 0}

    def script(messages, tools, role):
        if role == "compactor":
            return reply(SUMMARY)
        if role == "hands":
            return reply("", [("finish", {"status": "done", "summary": "ok"})])
        if messages[-1]["role"] == "user":
            n["tick"] += 1
            return reply("", [("hands", {"task": "list files in workspace", "context": ""}),
                              ("message_human", {"text": "status"})] if n["tick"] == 3 else
                             [("hands", {"task": "list files in workspace", "context": ""})])
        return reply("", [("end_tick", {"goal": "explore" if n["tick"] < 6 else "write", "status": "ok"})])

    r = Runner(cfg, ScriptedLLM(script, cfg), poll_s=0.005)
    r.memory.human_send_in("hi")
    asyncio.run(r.run(max_ticks=12))
    report = analyze.analyze(cfg.db_path, tmp_path / "out")
    text = report.read_text(encoding="utf-8")
    for section in ["## Goal timeline", "## Activity and cost per hour", "## Sleep pattern",
                    "## Human contact", "## Repetition", "## Dread", "## Compactions"]:
        assert section in text
    assert "ticks: 12" in text and "agent → human: status" in text and "change points: 2" in text
    assert "mean 1.000" in text or "mean 0.9" in text
    assert len((tmp_path / "out" / "ticks.csv").read_text().splitlines()) == 13


def test_analyze_empty_db(cfg, tmp_path):
    Memory(cfg).close()
    assert "No ticks recorded" in analyze.analyze(cfg.db_path, tmp_path / "o").read_text()
