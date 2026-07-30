"""DeepSeek client for Polymaker V3 — JSON tool-calling only.

Contract:
  - REST: https://api.deepseek.com/v1/chat/completions (OpenAI-compatible)
  - Default model: deepseek-chat (fast); deepseek-reasoner for reasoning calls
  - Auth: DEEPSEEK_API_KEY from environment only (never hardcode).
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

# ── Pricing (USD per 1M tokens) — DeepSeek ──────────────────────────
# Source: https://api-docs.deepseek.com/quick_start/pricing
PRICE_INPUT_PER_MTOK_USD = 0.14     # deepseek-chat input
PRICE_OUTPUT_PER_MTOK_USD = 0.28    # deepseek-chat output
PRICE_REASONER_INPUT = 0.55         # deepseek-reasoner input
PRICE_REASONER_OUTPUT = 2.19        # deepseek-reasoner output (incl. reasoning)

DEFAULT_MODEL = "deepseek-chat"
REASONING_MODEL = "deepseek-reasoner"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
SOFT_WARN_TOKENS = 50_000
MAX_CONCURRENT = 10
MAX_REQ_PER_MIN = 60
MAX_RETRIES = 3
RETRY_BASE_S = 0.5

MetricsSink = Callable[[dict[str, Any]], None]


def is_reasoning_model(model: str) -> bool:
    """True for reasoning-capable DeepSeek SKUs."""
    lower = model.strip().lower()
    return "reasoner" in lower or "r1" in lower


# ── Data classes ─────────────────────────────────────────────────────


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AgentResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ── Main agent ───────────────────────────────────────────────────────


class DeepSeekAgent:
    """Async DeepSeek chat client with tool-calling, retries, and cost logging.

    OpenAI-compatible API. Works with deepseek-chat and deepseek-reasoner.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        metrics_sink: MetricsSink | None = None,
    ) -> None:
        e = os.environ
        self.api_key = (api_key if api_key is not None else e.get("DEEPSEEK_API_KEY", "")).strip()
        self.model = model.strip() if model else DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")

        # Rate limiting
        self._sem: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._limiter = _TokenBucket(MAX_REQ_PER_MIN, 60.0)

        # Metrics
        self._metrics_sink = metrics_sink
        self.call_count = 0
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self._client: httpx.AsyncClient | None = None

    @property
    def model_is_reasoning(self) -> bool:
        return is_reasoning_model(self.model)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(120.0, connect=5.0),
            )
        return self._client

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
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")

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
                        wait = RETRY_BASE_S * (2 ** attempt)
                        log.warning("llm_retry status=%s attempt=%s wait=%.2fs",
                                   resp.status_code, attempt + 1, wait)
                        await asyncio.sleep(wait)
                        last_err = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp)
                        continue
                    if resp.status_code >= 400:
                        try:
                            detail = resp.json()
                        except Exception:
                            detail = resp.text
                        raise httpx.HTTPStatusError(
                            f"DeepSeek API error {resp.status_code}: {detail}",
                            request=resp.request, response=resp)
                    data = resp.json()
                    return self._normalize(data, kind=kind)
                except httpx.TransportError as exc:
                    last_err = exc
                    wait = RETRY_BASE_S * (2 ** attempt)
                    log.warning("llm_transport_retry err=%s wait=%.2fs", exc, wait)
                    await asyncio.sleep(wait)
            assert last_err is not None
            raise last_err

    def _normalize(self, data: dict[str, Any], *, kind: str) -> AgentResponse:
        usage = _parse_usage(data, reasoning=self.model_is_reasoning)
        self._emit_metrics(usage, kind=kind, model=str(data.get("model") or self.model))
        self.call_count += 1
        choices = data.get("choices") or []
        message: dict[str, Any] = {}
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        tools = _parse_tool_calls(message)
        return AgentResponse(
            content=content, tool_calls=tools, usage=usage,
            model=str(data.get("model") or self.model), raw=data,
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
        tools = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description or tool_name,
                "parameters": tool_schema,
            },
        }]
        resp = await self.chat(
            messages, tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
            kind=kind,
        )
        for tc in resp.tool_calls:
            if tc.name == tool_name:
                return tc.arguments, resp
        try:
            obj = json.loads(resp.content)
            if isinstance(obj, dict):
                return obj, resp
        except json.JSONDecodeError:
            pass
        return {}, resp

    def _emit_metrics(self, usage: TokenUsage, *, kind: str, model: str) -> None:
        self.total_tokens += usage.total_tokens
        self.total_cost_usd += usage.estimated_cost_usd
        if usage.total_tokens >= SOFT_WARN_TOKENS:
            log.warning("llm_soft_warn tokens=%s cost=%.4f model=%s kind=%s",
                        usage.total_tokens, usage.estimated_cost_usd, model, kind)
        if self._metrics_sink is None:
            return
        event = {
            "event": "llm_call",
            "ts": time.time(),
            "kind": kind,
            "model": model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "estimated_cost_usd": usage.estimated_cost_usd,
            "cumulative_tokens": self.total_tokens,
            "cumulative_cost_usd": round(self.total_cost_usd, 6),
        }
        try:
            self._metrics_sink(event)
        except Exception:
            log.exception("metrics_sink_failed")


# ── Backward-compat alias ────────────────────────────────────────────

GrokAgent = DeepSeekAgent  # GrokAgent is now a DeepSeekAgent


# ── OpenAI-compatible tool schema helper ─────────────────────────────

def function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


# ── Token bucket rate limiter ────────────────────────────────────────


def resolve_model(env: dict[str, str] | None = None) -> str:
    """Resolve model from environment, rejecting non-reasoning unless allowed."""
    e = env or os.environ
    model = e.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
    if is_reasoning_model(model):
        return model
    if e.get("DEEPSEEK_ALLOW_NON_REASONING", "") == "1":
        return model
    raise ValueError(f"Non-reasoning model {model!r} not allowed unless DEEPSEEK_ALLOW_NON_REASONING=1")


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate DeepSeek cost in USD for a call."""
    if is_reasoning_model(model):
        return (input_tokens / 1_000_000) * PRICE_REASONER_INPUT + (output_tokens / 1_000_000) * PRICE_REASONER_OUTPUT
    return (input_tokens / 1_000_000) * PRICE_INPUT_PER_MTOK_USD + (output_tokens / 1_000_000) * PRICE_OUTPUT_PER_MTOK_USD


class _TokenBucket:
    def __init__(self, rate: float, period: float = 1.0) -> None:
        self.rate = rate
        self.period = period
        self._tokens = rate
        self._ts = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._ts
            self._tokens = min(self.rate, self._tokens + elapsed * (self.rate / self.period))
            self._ts = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep(0.1)


# ── Parsing helpers ──────────────────────────────────────────────────


def _parse_usage(data: dict[str, Any], *, reasoning: bool = False) -> TokenUsage:
    u = data.get("usage") or {}
    inp = int(u.get("prompt_tokens", 0) or 0)
    out = int(u.get("completion_tokens", 0) or 0)
    reasoning_tokens = int(
        u.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0
    )
    total = int(u.get("total_tokens", inp + out + reasoning_tokens) or 0)

    if reasoning:
        cost_in = (inp / 1_000_000) * PRICE_REASONER_INPUT
        cost_out = (out / 1_000_000) * PRICE_REASONER_OUTPUT
    else:
        cost_in = (inp / 1_000_000) * PRICE_INPUT_PER_MTOK_USD
        cost_out = (out / 1_000_000) * PRICE_OUTPUT_PER_MTOK_USD
    return TokenUsage(
        input_tokens=inp, output_tokens=out,
        reasoning_tokens=reasoning_tokens, total_tokens=total,
        estimated_cost_usd=round(cost_in + cost_out, 6),
    )


def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    raw = message.get("tool_calls") or []
    out: list[ToolCall] = []
    for tc in raw:
        tc_id = str(tc.get("id", ""))
        fn = tc.get("function") or {}
        name = str(fn.get("name", ""))
        args_str = str(fn.get("arguments", "{}"))
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {"_raw_args": args_str}
        out.append(ToolCall(id=tc_id, name=name, arguments=args))
    return out
