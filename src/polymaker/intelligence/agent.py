"""Grok 4.5 (xAI) client for Polymaker V3 — JSON tool-calling only.

Contract:
  - REST: https://api.x.ai/v1/chat/completions
  - Default model: grok-4-1-fast-reasoning (env XAI_MODEL may override, but
    non-reasoning ids are rejected unless XAI_ALLOW_NON_REASONING=1).
  - Auth: XAI_API_KEY from environment only (never hardcode).
  - Actions must arrive as tool calls / structured JSON — free text is
    commentary only.
  - Token policy: NO hard stop. Soft-warn when a call uses ≥50k total tokens.
  - Rate: ≤60 req/min, ≤10 concurrent (asyncio.Semaphore + token bucket).
  - Retry: 3× exponential backoff on 429/5xx; 4xx surfaces immediately.

Metrics: each call logs prompt/completion/reasoning/total tokens and
estimated USD. Optional metrics_sink(event: dict) for JSONL writers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("polymaker.intelligence.agent")

# ── Pricing (USD per 1M tokens) — grok-4-1-fast-reasoning ────────────────
# Source: xAI published list pricing for this model family (verify periodically).
PRICE_INPUT_PER_MTOK_USD = 0.20
PRICE_OUTPUT_PER_MTOK_USD = 0.50

DEFAULT_MODEL = "grok-4-1-fast-reasoning"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
SOFT_WARN_TOKENS = 50_000
MAX_CONCURRENT = 10
MAX_REQ_PER_MIN = 60
MAX_RETRIES = 3
RETRY_BASE_S = 0.5

MetricsSink = Callable[[dict[str, Any]], None]


def is_reasoning_model(model: str) -> bool:
    """True for reasoning-capable xAI SKUs (reject non-reasoning variants)."""
    lower = model.strip().lower()
    # Explicit non-reasoning labels must never pass (even if they contain
    # the substring "reasoning", e.g. "non-reasoning-chat").
    if "non-reasoning" in lower or "nonreasoning" in lower:
        return False
    if "reasoning" in lower:
        return True
    # grok-4 family is reasoning-capable per product defaults
    return lower.startswith("grok-4") or lower.startswith("grok4")


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    estimated_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "estimated_usd": round(self.estimated_usd, 8),
        }


@dataclass
class ToolCall:
    """One structured tool invocation from the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AgentResponse:
    """Normalized agent result."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = DEFAULT_MODEL
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_calls)


def estimate_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    input_per_mtok: float = PRICE_INPUT_PER_MTOK_USD,
    output_per_mtok: float = PRICE_OUTPUT_PER_MTOK_USD,
) -> float:
    """Estimate USD cost from token counts."""
    return (prompt_tokens / 1_000_000.0) * input_per_mtok + (
        completion_tokens / 1_000_000.0
    ) * output_per_mtok


def _parse_usage(data: dict[str, Any]) -> TokenUsage:
    u = data.get("usage") or {}
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    # Reasoning tokens may be nested under completion_tokens_details
    details = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    reasoning = int(
        u.get("reasoning_tokens")
        or details.get("reasoning_tokens")
        or 0
    )
    total = int(u.get("total_tokens") or (prompt + completion))
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        total_tokens=total,
        estimated_usd=estimate_cost_usd(prompt, completion),
    )


def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    out: list[ToolCall] = []
    raw_calls = message.get("tool_calls") or []
    for tc in raw_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = str(fn.get("name") or tc.get("name") or "")
        args_raw = fn.get("arguments") or tc.get("arguments") or "{}"
        if isinstance(args_raw, dict):
            args = args_raw
        else:
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {"_raw": str(args_raw)}
        if not isinstance(args, dict):
            args = {"_value": args}
        out.append(
            ToolCall(
                id=str(tc.get("id") or ""),
                name=name,
                arguments=args,
            )
        )
    return out


def resolve_model(env: dict[str, str] | None = None) -> str:
    """Resolve model id; reject silent non-reasoning downgrades."""
    e = env if env is not None else os.environ
    model = (e.get("XAI_MODEL") or e.get("POLYMAKER_XAI_MODEL") or DEFAULT_MODEL).strip()
    allow = (e.get("XAI_ALLOW_NON_REASONING") or "").strip() in ("1", "true", "yes")
    if not allow and not is_reasoning_model(model):
        raise ValueError(
            f"model {model!r} does not look like a reasoning SKU; "
            f"default is {DEFAULT_MODEL}. Set XAI_ALLOW_NON_REASONING=1 to override."
        )
    return model


class RateLimiter:
    """Sliding-window request limiter (max N requests per 60s)."""

    def __init__(self, max_per_min: int = MAX_REQ_PER_MIN) -> None:
        self.max_per_min = max_per_min
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            if len(self._times) >= self.max_per_min:
                wait = 60.0 - (now - self._times[0]) + 0.01
                await asyncio.sleep(max(wait, 0.01))
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
            self._times.append(time.monotonic())


class GrokAgent:
    """Async xAI chat client with tool-calling, retries, and cost logging."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 120.0,
        max_concurrent: int = MAX_CONCURRENT,
        max_req_per_min: int = MAX_REQ_PER_MIN,
        metrics_sink: MetricsSink | None = None,
        client: httpx.AsyncClient | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        e = env if env is not None else dict(os.environ)
        self.api_key = (api_key if api_key is not None else e.get("XAI_API_KEY") or "").strip()
        self.model = model or resolve_model(e)
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._sem = asyncio.Semaphore(max_concurrent)
        self._limiter = RateLimiter(max_req_per_min)
        self._metrics_sink = metrics_sink
        self._owns_client = client is None
        self._client = client
        self._soft_warns = 0
        self.call_count = 0

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_s),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _emit_metrics(self, usage: TokenUsage, *, kind: str, model: str) -> None:
        event = {
            "event": "llm_call",
            "ts": time.time(),
            "kind": kind,
            "model": model,
            **usage.as_dict(),
            "soft_warn": usage.total_tokens >= SOFT_WARN_TOKENS,
        }
        if usage.total_tokens >= SOFT_WARN_TOKENS:
            self._soft_warns += 1
            log.warning(
                "llm_token_soft_warn total_tokens=%s threshold=%s call=%s",
                usage.total_tokens,
                SOFT_WARN_TOKENS,
                kind,
            )
        log.info(
            "llm_call model=%s prompt=%s completion=%s reasoning=%s total=%s usd=%.6f",
            model,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.reasoning_tokens,
            usage.total_tokens,
            usage.estimated_usd,
        )
        if self._metrics_sink is not None:
            try:
                self._metrics_sink(event)
            except Exception:  # noqa: BLE001
                log.exception("metrics_sink_failed")

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.2,
        kind: str = "chat",
        extra_body: dict[str, Any] | None = None,
    ) -> AgentResponse:
        """Send a chat completion; return content + structured tool calls."""
        if not self.api_key:
            raise RuntimeError("XAI_API_KEY is not set")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if extra_body:
            body.update(extra_body)

        last_err: Exception | None = None
        async with self._sem:
            await self._limiter.acquire()
            client = await self._get_client()
            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.post("/chat/completions", json=body)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        wait = RETRY_BASE_S * (2**attempt)
                        log.warning(
                            "llm_retry status=%s attempt=%s wait=%.2fs",
                            resp.status_code,
                            attempt + 1,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        last_err = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                        continue
                    if resp.status_code >= 400:
                        # 4xx (other than 429): surface immediately
                        try:
                            detail = resp.json()
                        except Exception:  # noqa: BLE001
                            detail = resp.text
                        raise httpx.HTTPStatusError(
                            f"xAI API error {resp.status_code}: {detail}",
                            request=resp.request,
                            response=resp,
                        )
                    data = resp.json()
                    return self._normalize(data, kind=kind)
                except httpx.TransportError as exc:
                    last_err = exc
                    wait = RETRY_BASE_S * (2**attempt)
                    log.warning("llm_transport_retry err=%s wait=%.2fs", exc, wait)
                    await asyncio.sleep(wait)
            assert last_err is not None
            raise last_err

    def _normalize(self, data: dict[str, Any], *, kind: str) -> AgentResponse:
        usage = _parse_usage(data)
        self._emit_metrics(usage, kind=kind, model=str(data.get("model") or self.model))
        self.call_count += 1
        choices = data.get("choices") or []
        message: dict[str, Any] = {}
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        tools = _parse_tool_calls(message)
        return AgentResponse(
            content=content,
            tool_calls=tools,
            usage=usage,
            model=str(data.get("model") or self.model),
            raw=data,
        )

    async def chat_json_tool(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tool_name: str,
        tool_schema: dict[str, Any],
        kind: str = "tool",
        description: str = "",
    ) -> tuple[dict[str, Any], AgentResponse]:
        """Force a single named tool and return its parsed arguments + full response."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": description or tool_name,
                    "parameters": tool_schema,
                },
            }
        ]
        resp = await self.chat(
            messages,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
            kind=kind,
        )
        for tc in resp.tool_calls:
            if tc.name == tool_name:
                return tc.arguments, resp
        # Fallback: try parse content as JSON object
        try:
            obj = json.loads(resp.content)
            if isinstance(obj, dict):
                return obj, resp
        except json.JSONDecodeError:
            pass
        return {}, resp


# OpenAI-compatible tool schema helpers used by oversight/discovery
def function_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
