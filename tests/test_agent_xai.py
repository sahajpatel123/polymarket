"""Tests for GrokAgent — mocked HTTP only."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from polymaker.intelligence.agent import (
    DEFAULT_MODEL,
    SOFT_WARN_TOKENS,
    GrokAgent,
    estimate_cost_usd,
    resolve_model,
)


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return await self.handler(request, self.calls)


def _completion(
    *,
    content: str = "",
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    reasoning_tokens: int = 10,
    status: int = 200,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_name is not None:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(tool_args or {}),
                },
            }
        ]
    body = {
        "id": "chatcmpl-test",
        "model": DEFAULT_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }
    return httpx.Response(status, json=body)


@pytest.mark.asyncio
async def test_default_model_is_reasoning() -> None:
    assert "reasoning" in DEFAULT_MODEL
    assert resolve_model({"XAI_MODEL": DEFAULT_MODEL}) == DEFAULT_MODEL
    with pytest.raises(ValueError):
        resolve_model({"XAI_MODEL": "grok-mini-chat"})


@pytest.mark.asyncio
async def test_chat_tool_schema_and_cost_logging() -> None:
    events: list[dict[str, Any]] = []

    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content.decode())
        assert payload["model"] == DEFAULT_MODEL
        assert "tools" in payload
        return _completion(
            tool_name="oversight_report",
            tool_args={"narrative": "ok", "actions": [], "reasoning": "fine"},
            prompt_tokens=200,
            completion_tokens=100,
            reasoning_tokens=40,
        )

    transport = _MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")
    agent = GrokAgent(
        api_key="test-key-not-real",
        client=client,
        metrics_sink=events.append,
    )
    try:
        args, resp = await agent.chat_json_tool(
            [{"role": "user", "content": "hi"}],
            tool_name="oversight_report",
            tool_schema={"type": "object", "properties": {}},
            kind="test",
        )
        assert args["narrative"] == "ok"
        assert resp.has_tools
        assert resp.tool_calls[0].name == "oversight_report"
        assert resp.usage.prompt_tokens == 200
        assert resp.usage.completion_tokens == 100
        assert resp.usage.reasoning_tokens == 40
        assert resp.usage.total_tokens == 300
        expected = estimate_cost_usd(200, 100)
        assert abs(resp.usage.estimated_usd - expected) < 1e-12
        assert events and events[0]["event"] == "llm_call"
        assert events[0]["total_tokens"] == 300
        assert "estimated_usd" in events[0]
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_retry_on_429_then_success() -> None:
    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        if n < 3:
            return httpx.Response(429, json={"error": "rate"})
        return _completion(content="done", prompt_tokens=10, completion_tokens=5)

    transport = _MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")
    agent = GrokAgent(api_key="k", client=client)
    try:
        resp = await agent.chat([{"role": "user", "content": "x"}], kind="retry")
        assert resp.content == "done"
        assert transport.calls == 3
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_4xx_surfaces_without_infinite_retry() -> None:
    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    transport = _MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")
    agent = GrokAgent(api_key="k", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError) as ei:
            await agent.chat([{"role": "user", "content": "x"}])
        assert ei.value.response.status_code == 400
        assert transport.calls == 1
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_soft_warn_no_hard_cap_stop() -> None:
    events: list[dict[str, Any]] = []

    async def handler(request: httpx.Request, n: int) -> httpx.Response:
        return _completion(
            content="huge",
            prompt_tokens=SOFT_WARN_TOKENS,
            completion_tokens=1000,
            reasoning_tokens=500,
        )

    transport = _MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.x.ai/v1")
    agent = GrokAgent(api_key="k", client=client, metrics_sink=events.append)
    try:
        resp = await agent.chat([{"role": "user", "content": "big"}])
        assert resp.content == "huge"
        assert resp.usage.total_tokens >= SOFT_WARN_TOKENS
        assert events[0]["soft_warn"] is True
        assert agent._soft_warns >= 1
        # No exception = no hard stop
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    agent = GrokAgent(api_key="", env={"XAI_API_KEY": ""})
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        await agent.chat([{"role": "user", "content": "x"}])


def test_semaphore_and_rate_bounds_exist() -> None:
    agent = GrokAgent(api_key="k", max_concurrent=10, max_req_per_min=60)
    assert agent._sem._value == 10  # noqa: SLF001
    assert agent._limiter.max_per_min == 60
