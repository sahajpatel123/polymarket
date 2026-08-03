"""Tests for the DeepSeek agent client.

This file previously targeted an xAI/Grok client that no longer exists — it
expected a ``client=`` constructor kwarg, ``XAI_API_KEY`` and
``grok-4-1-fast-reasoning``. Those 7 failures masked real regressions in every
run, so the behaviours are ported here against the actual ``DeepSeekAgent`` API:
retry on 429, 4xx surfacing without infinite retry, cost accounting, the
soft token warning, missing-key handling, and the rate-limit bounds.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from polymaker.intelligence.agent import (
    DEFAULT_MODEL,
    MAX_CONCURRENT,
    MAX_REQ_PER_MIN,
    REASONING_MODEL,
    SOFT_WARN_TOKENS,
    DeepSeekAgent,
    estimate_cost_usd,
    is_reasoning_model,
    resolve_model,
)


def _completion(
    *,
    content: str = "",
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    reasoning_tokens: int = 10,
    status: int = 200,
    model: str = DEFAULT_MODEL,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_name is not None:
        message["tool_calls"] = [{
            "id": "call_1",
            "type": "function",
            "function": {"name": tool_name,
                         "arguments": json.dumps(tool_args or {})},
        }]
    body = {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }
    return httpx.Response(status, json=body)


def _agent_with(handler, **kw: Any) -> DeepSeekAgent:
    """Build an agent whose httpx client is backed by a mock transport."""
    calls = {"n": 0}

    async def _handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return await handler(request, calls["n"])

    agent = DeepSeekAgent(api_key="test-key", **kw)
    agent._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        base_url=agent.base_url,
        transport=httpx.MockTransport(_handle),
        headers={"Authorization": "Bearer test-key"},
    )
    agent._calls_seen = calls  # type: ignore[attr-defined]
    return agent


# ── model policy ─────────────────────────────────────────────────────────


def test_default_model_is_the_cheap_sku() -> None:
    """The oversight loop runs every 30s; the reasoner costs 4-8x more."""
    assert DEFAULT_MODEL == "deepseek-chat"
    assert not is_reasoning_model(DEFAULT_MODEL)


def test_resolve_model_defaults_to_a_reasoning_sku() -> None:
    """Regression: resolve_model defaulted to deepseek-chat and then rejected it,
    so it raised ValueError on a clean environment."""
    assert resolve_model({}) == REASONING_MODEL
    assert is_reasoning_model(resolve_model({}))


def test_resolve_model_rejects_non_reasoning_without_opt_in() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        resolve_model({"DEEPSEEK_MODEL": "deepseek-chat"})
    assert resolve_model({
        "DEEPSEEK_MODEL": "deepseek-chat",
        "DEEPSEEK_ALLOW_NON_REASONING": "1",
    }) == "deepseek-chat"


def test_is_reasoning_model_avoids_the_substring_trap() -> None:
    assert is_reasoning_model("deepseek-reasoner")
    assert not is_reasoning_model("non-reasoning")
    with pytest.raises(ValueError):
        resolve_model({"DEEPSEEK_MODEL": "non-reasoning"})


def test_reasoner_costs_more_than_chat() -> None:
    cheap = estimate_cost_usd(DEFAULT_MODEL, 1_000_000, 1_000_000)
    dear = estimate_cost_usd(REASONING_MODEL, 1_000_000, 1_000_000)
    assert dear > cheap > 0


# ── request shape + accounting ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_sends_tools_and_records_cost() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content.decode())
        return _completion(tool_name="oversight_report",
                           tool_args={"narrative": "ok", "actions": []},
                           prompt_tokens=200, completion_tokens=100)

    agent = _agent_with(handler)
    tools = [{"type": "function", "function": {"name": "oversight_report",
                                              "parameters": {"type": "object"}}}]
    resp = await agent.chat([{"role": "user", "content": "hi"}], tools=tools)

    assert seen["path"].endswith("/chat/completions")
    assert seen["payload"]["model"] == DEFAULT_MODEL
    assert "tools" in seen["payload"]
    assert resp.tool_calls and resp.tool_calls[0].name == "oversight_report"
    assert resp.tool_calls[0].arguments["narrative"] == "ok"
    assert resp.usage.total_tokens == 300
    assert agent.call_count == 1
    assert agent.total_cost_usd > 0


@pytest.mark.asyncio
async def test_metrics_sink_receives_a_cost_event() -> None:
    events: list[dict[str, Any]] = []

    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        return _completion(content="ok")

    agent = _agent_with(handler, metrics_sink=events.append)
    await agent.chat([{"role": "user", "content": "hi"}])
    assert events, "no metrics emitted for an LLM call"
    assert any("cost" in json.dumps(e) or "tokens" in json.dumps(e)
               for e in events)


# ── retry / error semantics ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_on_429_then_success() -> None:
    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        if n == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _completion(content="recovered")

    agent = _agent_with(handler)
    resp = await agent.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "recovered"
    assert agent._calls_seen["n"] >= 2, "429 was not retried"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_4xx_surfaces_without_infinite_retry() -> None:
    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    agent = _agent_with(handler)
    with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
        await agent.chat([{"role": "user", "content": "hi"}])
    assert agent._calls_seen["n"] <= 4, (  # type: ignore[attr-defined]
        "a 400 must not be retried indefinitely"
    )


@pytest.mark.asyncio
async def test_soft_warn_does_not_hard_stop() -> None:
    """A large prompt warns but must still complete — never silently drop work."""
    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        return _completion(content="ok", prompt_tokens=SOFT_WARN_TOKENS + 1_000)

    agent = _agent_with(handler)
    resp = await agent.chat([{"role": "user", "content": "x" * 100}])
    assert resp.content == "ok"
    assert agent.total_tokens > SOFT_WARN_TOKENS


@pytest.mark.asyncio
async def test_missing_api_key_raises_naming_the_right_var() -> None:
    agent = DeepSeekAgent(api_key="")
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        await agent.chat([{"role": "user", "content": "hi"}])


def test_rate_limit_bounds_exist() -> None:
    assert MAX_CONCURRENT >= 1
    assert MAX_REQ_PER_MIN >= 1
