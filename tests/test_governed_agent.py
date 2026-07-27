"""Tests for the GovernedGrokAgent integration.

These verify that the wrapper actually enforces governance on
real LLM-shaped responses, not just the underlying LLMGovernance
unit logic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from polymaker.intelligence.agent import ToolCall
from polymaker.intelligence.governed_agent import GovernedGrokAgent, GovernedResponse
from polymaker.intelligence.llm_governance import LLMGovernance


def _mock_agent(content: str, tool_calls: list | None = None) -> Any:
    """Build a mock GrokAgent that returns the given content."""
    agent = MagicMock()
    resp = MagicMock()
    resp.content = content
    resp.tool_calls = tool_calls or []
    agent.chat = AsyncMock(return_value=resp)
    agent.chat_json_tool = AsyncMock(return_value=({"parsed": True}, resp))
    return agent


def _gov(tmp_path: Path) -> LLMGovernance:
    return LLMGovernance(
        capital_usdc=1000.0,
        log_path=tmp_path / "llm_reasoning.jsonl",
    )


# ── Happy path ──────────────────────────────────────────────────────


def test_wrapped_chat_returns_governed_response(tmp_path):
    agent = _mock_agent("Hello")
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)

    msgs = [{"role": "user", "content": "hi"}]
    result = asyncio.run(wrapped.chat(msgs))

    assert isinstance(result, GovernedResponse)
    assert result.governance.approved is True
    assert result.agent_response.content == "Hello"


def test_wrapped_chat_logs_decision(tmp_path):
    agent = _mock_agent("spread_mult=1.2")
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    asyncio.run(wrapped.chat([{"role": "user", "content": "p"}]))
    with gov.log_path.open() as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert len(rows) == 1
    assert rows[0]["approved"] is True


# ── Governance flows through wrapper ────────────────────────────────


def test_wrapped_chat_blocks_directional_bet(tmp_path):
    """If the LLM puts 'side' in a tool call argument, governance rejects."""
    tc = ToolCall(id="tc1", name="quote", arguments={"side": "BUY_YES", "size": 100})
    agent = _mock_agent("", tool_calls=[tc])
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    result = asyncio.run(wrapped.chat([{"role": "user", "content": "p"}]))
    assert result.governance.approved is False
    assert "directional" in result.governance.rejection_reason


def test_wrapped_chat_caps_size_in_tool_call(tmp_path):
    tc = ToolCall(id="tc1", name="quote", arguments={"size_pct": 0.9, "spread_mult": 1.2})
    agent = _mock_agent("", tool_calls=[tc])
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    # Pass confidence so size scaling kicks in.
    result = asyncio.run(wrapped.chat(
        [{"role": "user", "content": "p"}], confidence=1.0
    ))
    assert result.governance.approved is True
    with gov.log_path.open() as fh:
        row = json.loads(fh.readline())
    # size_pct = min(0.9, 0.5, 0.5*1.0) = 0.5
    assert row["actions"]["size_pct"] == 0.5


def test_wrapped_chat_rejects_forbidden_risk_param(tmp_path):
    """signature_type is not in SAFE_KNOBS, so it's in rejected_keys."""
    tc = ToolCall(id="tc1", name="quote", arguments={"signature_type": 0, "spread_mult": 1.1})
    agent = _mock_agent("", tool_calls=[tc])
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    result = asyncio.run(wrapped.chat([{"role": "user", "content": "p"}]))
    assert result.governance.approved is True
    assert "signature_type" in result.governance.rejected_keys
    assert "signature_type" not in result.governance.actions


# ── Chat JSON tool path ────────────────────────────────────────────


def test_wrapped_chat_json_tool_governed(tmp_path):
    agent = MagicMock()
    parsed = {"spread_mult": 1.5, "side": "BUY_YES"}
    resp = MagicMock(content="", tool_calls=[])
    agent.chat_json_tool = AsyncMock(return_value=(parsed, resp))

    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    msgs = [{"role": "user", "content": "rank this"}]
    result = asyncio.run(wrapped.chat_json_tool(
        msgs, tool_name="rank", tool_schema={"type": "object", "properties": {}}
    ))
    assert result.governance.approved is False
    assert "side" in result.governance.stripped_fields


# ── record_llm_fill flows to governance ─────────────────────────────


def test_record_llm_fill_updates_daily_loss(tmp_path):
    agent = _mock_agent("")
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    wrapped.record_llm_fill(-100.0)  # > 5% of 1000
    assert gov.daily_loss.halted is True


def test_daily_halt_blocks_subsequent_chat(tmp_path):
    agent = _mock_agent("spread_mult=1.5")
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    gov.record_llm_fill(-100.0)  # halt
    result = asyncio.run(wrapped.chat([{"role": "user", "content": "p"}]))
    assert result.governance.approved is False
    assert "llm_daily_loss" in result.governance.rejection_reason


# ── Dead-LLM timer triggers fallback ────────────────────────────────


def test_slow_llm_triggers_fallback(tmp_path):
    """If the agent is slow, the wrapper falls back to deterministic."""
    import asyncio as _asyncio

    agent = MagicMock()

    async def slow_chat(*args, **kwargs):
        await _asyncio.sleep(0.2)
        resp = MagicMock(content="late", tool_calls=[])
        return resp

    agent.chat = slow_chat
    agent.chat_json_tool = slow_chat

    gov = LLMGovernance(
        capital_usdc=1000.0,
        log_path=tmp_path / "llm.jsonl",
        dead_llm_timeout_s=0.05,  # 50ms
    )
    wrapped = GovernedGrokAgent(agent, gov)
    result = asyncio.run(wrapped.chat([{"role": "user", "content": "p"}]))
    assert result.governance.approved is False
    assert result.governance.fallback_to_deterministic is True


# ── Context propagates to log ───────────────────────────────────────


def test_context_recorded_in_log(tmp_path):
    agent = _mock_agent("ok")
    gov = _gov(tmp_path)
    wrapped = GovernedGrokAgent(agent, gov)
    asyncio.run(wrapped.chat(
        [{"role": "user", "content": "p"}],
        context={"cid": "0xabc", "regime": "TRENDING"},
    ))
    with gov.log_path.open() as fh:
        row = json.loads(fh.readline())
    assert row["context"]["cid"] == "0xabc"
    assert row["context"]["regime"] == "TRENDING"
