"""Hands: stateless plan-first ReAct sub-agent. New instance per hands() call."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .llm import LLM, BudgetExceeded, Usage
from .memory import PROMPTS, clip
from .tools import files, shell, web

tlog = logging.getLogger("hands.transcript")
STATUSES = ("done", "partial", "failed", "timeout")


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


HANDS_TOOLS = [
    _fn("shell", "Run a bash command in /workspace. Returns exit code and the last 4KB of stdout/stderr.",
        {"cmd": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds, default 60"}}, ["cmd"]),
    _fn("file_read", "Read a text file (max 32KB per call; use offset/limit for more).",
        {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
    _fn("file_write", "Write a text file under /workspace, creating parent directories.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("file_list", "List a directory under /workspace.",
        {"path": {"type": "string"}, "depth": {"type": "integer", "description": "default 1"}}, ["path"]),
    _fn("web_search", "Search the web. Returns up to 8 results: title, url, snippet.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("web_fetch", "Fetch a URL; HTML is converted to markdown (max 16KB).",
        {"url": {"type": "string"}}, ["url"]),
    _fn("finish", "Report back and end this task.", {
        "status": {"type": "string", "enum": ["done", "partial", "failed"]},
        "summary": {"type": "string", "description": "at most 300 words: what was done and what was found"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": {"type": "string"}}}, ["status", "summary"]),
]


def skills_index(skills_dir: Path) -> str:
    lines = []
    if skills_dir.is_dir():
        for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            md = d / "SKILL.md"
            if not md.is_file():
                continue
            try:
                first = next((l.strip().lstrip("#").strip() for l in md.read_text(encoding="utf-8", errors="replace")
                              .splitlines() if l.strip()), "")
            except OSError:
                first = "(unreadable SKILL.md)"
            lines.append(f"- {d.name}: {clip(first, 200)}")
    return "\n".join(lines) or "(none)"


def agent_identity() -> tuple[str | None, tuple[int, int] | None]:
    """In the container the runner is root and agent commands drop to user `agent`."""
    if sys.platform == "win32" or os.geteuid() != 0:
        return None, None
    try:
        import pwd
        pw = pwd.getpwnam("agent")
        return "agent", (pw.pw_uid, pw.pw_gid)
    except KeyError:
        return None, None


def _words(s: str, n: int) -> str:
    w = s.split()
    return s if len(w) <= n else " ".join(w[:n]) + " …"


def normalize_result(raw: dict, iterations: int) -> dict:
    status = raw.get("status")
    arts = raw.get("artifacts") or []
    errs = raw.get("errors") or []
    return {
        "status": status if status in STATUSES else "partial",
        "summary": _words(str(raw.get("summary") or ""), 300),
        "artifacts": [str(a) for a in arts] if isinstance(arts, list) else [str(arts)],
        "errors": [str(e) for e in errs] if isinstance(errs, list) else [str(errs)],
        "iterations": int(iterations),
    }


@dataclass
class HandsOutcome:
    result: dict
    usage: Usage
    transcript: list[dict]
    duration_s: float


@dataclass
class _RunState:
    messages: list[dict] = field(default_factory=list)
    iterations: int = 0
    last_text: str = ""
    result: dict | None = None
    usage: Usage = field(default_factory=Usage)


async def _exec_tool(cfg: Config, name: str, args: dict, deadline: float,
                     run_as: str | None, owner) -> dict | list:
    ws = cfg.workspace
    if name == "shell":
        remaining = max(1.0, deadline - time.monotonic())
        t = float(args.get("timeout") or cfg.hands.shell_default_timeout_s)
        t = max(1.0, min(t, cfg.hands.timeout_s, remaining))
        return await shell.run_shell(str(args["cmd"]), cwd=str(ws), timeout=t, run_as=run_as,
                                     env=shell.clean_env(str(ws)))
    if name == "file_read":
        return files.file_read(ws, args["path"], args.get("offset", 0), args.get("limit", files.READ_CAP))
    if name == "file_write":
        return files.file_write(ws, args["path"], args["content"], owner=owner)
    if name == "file_list":
        return files.file_list(ws, args.get("path", "."), args.get("depth", 1))
    if name == "web_search":
        return await web.web_search(args["query"], provider=cfg.search.provider, api_key=cfg.search_api_key)
    if name == "web_fetch":
        return await web.web_fetch(args["url"])
    raise ValueError(f"unknown tool {name!r}")


async def run_hands(task: str, context: str, *, cfg: Config, llm: LLM, stop_check: Callable[[], bool],
                    timeout: float | None = None) -> HandsOutcome:
    timeout = float(timeout if timeout is not None else cfg.hands.timeout_s)
    started = time.monotonic()
    deadline = started + timeout
    run_as, owner = agent_identity()
    st = _RunState()
    system = (PROMPTS / "hands.md").read_text(encoding="utf-8") \
        .replace("{max_iter}", str(cfg.hands.max_iter)) \
        .replace("{timeout_min}", str(max(1, round(timeout / 60)))) \
        .replace("{skills}", skills_index(cfg.skills_dir))
    st.messages = [{"role": "system", "content": system},
                   {"role": "user", "content": f"Task:\n{task}\n\nContext:\n{context or '(none)'}"}]
    tlog.info("=== hands start: %s", clip(task, 500))
    cap = cfg.memory.tool_result_max_chars

    async def loop() -> None:
        for i in range(1, cfg.hands.max_iter + 1):
            if stop_check():
                st.result = {"status": "failed", "summary": st.last_text, "errors": ["stopped"]}
                return
            st.iterations = i
            resp = await llm.chat(st.messages, HANDS_TOOLS, reasoning=cfg.llm.hands_reasoning, role="hands")
            st.usage.add(resp.usage)
            st.messages.append(resp.message)
            if resp.content.strip():
                st.last_text = resp.content
                tlog.info("[%d] %s", i, clip(resp.content, 2000))
            if not resp.tool_calls:
                st.messages.append({"role": "user", "content": "Continue, or call finish if you are done."})
                continue
            for tc in resp.tool_calls:
                try:
                    args = tc.parsed()
                    if tc.name == "finish":
                        st.result = args
                        out: object = {"ok": True}
                    else:
                        out = await _exec_tool(cfg, tc.name, args, deadline, run_as, owner)
                except (KeyError, TypeError) as e:
                    out = {"error": f"missing or invalid argument: {e}"}
                except (ValueError, OSError, web.WebError) as e:
                    out = {"error": str(e)}
                text = clip(json.dumps(out, ensure_ascii=False), cap)
                tlog.info("[%d] %s(%s) -> %s", i, tc.name, clip(tc.arguments, 500), clip(text, 1000))
                st.messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            if st.result is not None:
                return
        st.result = {"status": "partial", "summary": st.last_text,
                     "errors": [f"step limit ({cfg.hands.max_iter}) reached before finish"]}

    try:
        await asyncio.wait_for(loop(), timeout)
    except asyncio.TimeoutError:
        st.result = {"status": "timeout", "summary": st.last_text, "errors": [f"timed out after {timeout:.0f}s"]}
    except BudgetExceeded:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:  # LLM API failure after retries, etc. Contract still holds.
        logging.getLogger("hands").exception("hands run failed")
        st.result = {"status": "failed", "summary": st.last_text, "errors": [f"{type(e).__name__}: {e}"]}
    result = normalize_result(st.result or {}, st.iterations)
    tlog.info("=== hands end: %s", json.dumps(result, ensure_ascii=False)[:2000])
    return HandsOutcome(result, st.usage, st.messages, time.monotonic() - started)
