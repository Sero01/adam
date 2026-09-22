"""LLM client wrapper: one chat() call, usage -> cost, budget guard."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .config import Config

log = logging.getLogger("llm")


class BudgetExceeded(Exception):
    pass


@dataclass
class Usage:
    tokens_in: int = 0       # total prompt tokens (includes cached)
    tokens_out: int = 0      # completion tokens (includes reasoning)
    tokens_cached: int = 0   # prompt tokens read from cache
    cost_usd: float = 0.0
    calls: int = 0
    provider: str = ""       # upstream that served a single call (OpenRouter); not aggregated

    def add(self, other: "Usage") -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.tokens_cached += other.tokens_cached
        self.cost_usd += other.cost_usd
        self.calls += other.calls


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model

    def parsed(self) -> dict:
        """Parse arguments; raises ValueError with a model-readable message."""
        try:
            val = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"arguments are not valid JSON ({e.msg}); raw: {self.arguments[:200]}")
        if not isinstance(val, dict):
            raise ValueError("arguments must be a JSON object")
        return val


@dataclass
class LLMResponse:
    message: dict[str, Any]           # assistant message, ready to append to history
    content: str
    tool_calls: list[ToolCall]
    usage: Usage
    finish_reason: str | None = None
    reasoning: str = ""               # reasoning text if the provider returns it (logged, not replayed)


class LLM(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict] | None, *,
                   reasoning: str, role: str) -> LLMResponse: ...


def cost_from_tokens(cfg: Config, tokens_in: int, tokens_out: int, tokens_cached: int) -> float:
    p = cfg.pricing
    uncached = max(tokens_in - tokens_cached, 0)
    return (uncached * p.input + tokens_cached * p.cached_input + tokens_out * p.output) / 1e6


class Meter:
    """Global spend. Checked before every LLM call; persisted after every call."""

    def __init__(self, cfg: Config, spent: float, persist: Callable[[float], None]):
        self.cfg = cfg
        self.spent = spent
        self._persist = persist
        self._warned = spent >= cfg.budget.soft_warn_usd

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.cfg.budget.hard_cap_usd

    def check(self) -> None:
        if self.exhausted:
            raise BudgetExceeded(f"spent ${self.spent:.4f} >= hard cap ${self.cfg.budget.hard_cap_usd:.2f}")

    def charge(self, usage: Usage) -> None:
        self.spent += usage.cost_usd
        self._persist(self.spent)
        if not self._warned and self.spent >= self.cfg.budget.soft_warn_usd:
            self._warned = True
            log.warning("soft budget warning: spent $%.4f (soft $%.2f, hard $%.2f)",
                        self.spent, self.cfg.budget.soft_warn_usd, self.cfg.budget.hard_cap_usd)


class MeteredLLM:
    """Wraps any LLM: budget check before, charge after. Every call is costed."""

    def __init__(self, inner: LLM, meter: Meter,
                 on_call: Callable[[str, Usage], None] | None = None):
        self.inner = inner
        self.meter = meter
        self.on_call = on_call

    async def chat(self, messages, tools, *, reasoning, role) -> LLMResponse:
        self.meter.check()
        resp = await self.inner.chat(messages, tools, reasoning=reasoning, role=role)
        self.meter.charge(resp.usage)
        if self.on_call:
            self.on_call(role, resp.usage)
        return resp


class OpenAIChatLLM:
    """OpenAI-compatible endpoint (OpenRouter default, Z.ai direct works)."""

    def __init__(self, cfg: Config):
        from openai import AsyncOpenAI  # imported lazily so tests don't need a key

        self.cfg = cfg
        self.client = AsyncOpenAI(api_key=cfg.llm_api_key or "missing", base_url=cfg.llm.base_url,
                                  timeout=cfg.llm.request_timeout_s, max_retries=0)
        url = cfg.llm.base_url.lower()
        self.is_openrouter = "openrouter.ai" in url
        self.is_zai = "z.ai" in url or "bigmodel.cn" in url

    def _extra(self, reasoning: str) -> tuple[dict, dict]:
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if self.is_openrouter:
            # OpenRouter's documented form is reasoning:{effort}; top-level reasoning_effort is not.
            extra["reasoning"] = {"effort": reasoning}
            pv = self.cfg.llm.provider
            if pv.order:
                extra["provider"] = {"order": pv.order, "allow_fallbacks": pv.allow_fallbacks,
                                     "require_parameters": pv.require_parameters}
        elif self.is_zai:
            # Z.ai has no effort levels, only thinking on/off.
            extra["thinking"] = {"type": "disabled" if reasoning in ("none", "minimal") else "enabled"}
        else:
            kwargs["reasoning_effort"] = reasoning
        return kwargs, extra

    async def chat(self, messages, tools, *, reasoning, role) -> LLMResponse:
        from openai import (APIConnectionError, APIStatusError, APITimeoutError,
                            InternalServerError, RateLimitError)

        kwargs, extra = self._extra(reasoning)
        if tools:
            kwargs["tools"] = tools
        attempt = 0
        while True:
            try:
                resp = await self.client.chat.completions.create(
                    model=self.cfg.llm.model, messages=messages,
                    max_tokens=self.cfg.llm.max_output_tokens,
                    extra_body=extra or None, **kwargs)
                if not resp.choices:
                    raise InternalServerError("empty choices", response=_FakeResp(), body=None)
                break
            except (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError) as e:
                retryable = True
                err = e
            except APIStatusError as e:  # 4xx other than 429: provider down under pinning shows as 404/400
                retryable = e.status_code in (402, 404, 408, 409, 425) and attempt < 1
                err = e
            attempt += 1
            if not retryable or attempt > self.cfg.llm.max_retries:
                raise err
            delay = min(60, 2 ** attempt) + random.random()
            log.warning("%s call failed (%s), retry %d in %.1fs", role, type(err).__name__, attempt, delay)
            await asyncio.sleep(delay)
        return self._parse(resp)

    def _parse(self, resp) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = [ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}")
                      for tc in (msg.tool_calls or [])]
        out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            out["tool_calls"] = [{"id": t.id, "type": "function",
                                  "function": {"name": t.name, "arguments": t.arguments}}
                                 for t in tool_calls]
        extras = getattr(msg, "model_extra", None) or {}
        if extras.get("reasoning_details"):
            # OpenRouter: must be passed back unchanged during a tool loop.
            out["reasoning_details"] = extras["reasoning_details"]
        u = resp.usage
        usage = Usage(calls=1, provider=str((getattr(resp, "model_extra", None) or {}).get("provider") or ""))
        if u is not None:
            usage.tokens_in = u.prompt_tokens or 0
            usage.tokens_out = u.completion_tokens or 0
            details = getattr(u, "prompt_tokens_details", None)
            usage.tokens_cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
            reported = (getattr(u, "model_extra", None) or {}).get("cost")
            if reported is not None:
                usage.cost_usd = float(reported)  # actual charge; survives price/promo changes
            else:
                usage.cost_usd = cost_from_tokens(self.cfg, usage.tokens_in, usage.tokens_out, usage.tokens_cached)
        else:
            log.error("response without usage; call not costed by provider, estimating 0 tokens")
        reasoning = extras.get("reasoning") or extras.get("reasoning_content") or ""
        return LLMResponse(out, msg.content or "", tool_calls, usage, choice.finish_reason, str(reasoning))


class _FakeResp:  # minimal stand-in so InternalServerError can be constructed
    status_code = 500
    headers: dict = {}
    request = None


# ---------------------------------------------------------------- test double

ScriptFn = Callable[[list[dict], list[dict] | None, str], "LLMResponse | Awaitable[LLMResponse]"]


class ScriptedLLM:
    """Deterministic fake. `script(messages, tools, role)` returns an LLMResponse."""

    def __init__(self, script: ScriptFn, cfg: Config | None = None,
                 tokens_in: int = 1000, tokens_out: int = 100):
        self.script = script
        self.cfg = cfg
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, messages, tools, *, reasoning, role) -> LLMResponse:
        self.calls.append((role, [dict(m) for m in messages]))
        r = self.script(messages, tools, role)
        if asyncio.iscoroutine(r):
            r = await r
        if r.usage.calls == 0:
            cost = cost_from_tokens(self.cfg, self.tokens_in, self.tokens_out, 0) if self.cfg else 0.001
            r.usage = Usage(self.tokens_in, self.tokens_out, 0, cost, 1)
        return r


def reply(content: str = "", tool_calls: list[tuple[str, dict]] | None = None) -> LLMResponse:
    """Build a fake assistant response. tool_calls: [(name, args), ...]."""
    tcs = [ToolCall(f"call_{random.randrange(1 << 30)}", n, json.dumps(a)) for n, a in (tool_calls or [])]
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tcs:
        msg["tool_calls"] = [{"id": t.id, "type": "function",
                              "function": {"name": t.name, "arguments": t.arguments}} for t in tcs]
    return LLMResponse(msg, content, tcs, Usage(), "tool_calls" if tcs else "stop")
