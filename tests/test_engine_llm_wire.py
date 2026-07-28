"""Shipped engine LLM wiring: key present → stack on; key absent → fallback.

Drives production paths:
  - :meth:`Engine.wire_llm_stack`
  - :meth:`Engine.run_oversight_cycle_once` (same pack/apply as _oversight_loop_task)
  - :meth:`Engine.run_llm_discovery_cycle_once` / ``_apply_llm_rankings``
  - :meth:`Engine.apply_oversight_action` via packed payloads only
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from polymaker.config import RiskConfig, Secrets, StrategyProfile
from polymaker.engine import Engine
from polymaker.intelligence.agent import AgentResponse, TokenUsage, ToolCall
from polymaker.intelligence.llm_governance import LLMGovernance
from tests.test_engine import _engine_with_market, _feed_book


@dataclass
class FakeGrok:
    """Minimal agent surface for MarketDiscovery / OversightLoop."""

    model: str = "mock-grok"
    calls: int = 0
    rank_payload: dict[str, Any] | None = None
    oversight_payload: dict[str, Any] | None = None

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        return AgentResponse(
            content="ok",
            tool_calls=[],
            usage=TokenUsage(1, 1, 2),
        )

    async def chat_json_tool(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        tool_name = kwargs.get("tool_name") or ""
        if "rank" in tool_name or tool_name == "rank_markets":
            payload = self.rank_payload or {
                "rankings": [
                    {
                        "condition_id": "0xcond",
                        "confidence": 0.8,
                        "narrative": "ok",
                        "suggested_size_pct": 0.2,
                        "risk_notes": "",
                    }
                ]
            }
        else:
            payload = self.oversight_payload or {
                "narrative": "calm book",
                "reasoning": "fill rate normal",
                "actions": [
                    {
                        "type": "widen_spread",
                        "market": "0xcond",
                        "params": {"mult": 1.2},
                        "dry_run": False,
                        "reason": "test",
                    }
                ],
            }
        resp = AgentResponse(
            content="tool",
            tool_calls=[ToolCall(id="1", name=tool_name or "tool", arguments=payload)],
            usage=TokenUsage(10, 5, 15),
        )
        return payload, resp


def test_wire_llm_skipped_without_api_key(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.secrets = Secrets()  # empty XAI_API_KEY
    eng._llm_enabled = bool(eng.cfg.secrets.xai_api_key)
    ok = eng.wire_llm_stack()
    assert ok is False
    assert eng.oversight_loop is None
    assert eng.gov_agent is None
    assert eng._discovery_agent is None
    _feed_book(eng, meta)
    import asyncio

    asyncio.run(eng._recompute(meta.condition_id))
    eng.state.close()
    eng.catalog.close()


def test_wire_llm_with_mock_agent_and_capital(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=1000.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.cfg.profiles = {"default": StrategyProfile()}
    fake = FakeGrok()
    eng._llm_enabled = True
    ok = eng.wire_llm_stack(agent=fake, force_capital_usdc=1000.0)
    assert ok is True
    assert eng.llm_gov is not None
    assert eng.gov_agent is not None
    assert eng.oversight_loop is not None
    assert eng._discovery_agent is not None
    assert eng._gov_facade is not None
    assert eng.memory is not None
    eng.state.close()
    eng.catalog.close()
    eng.memory.close()


@pytest.mark.asyncio
async def test_run_oversight_cycle_once_applies_params_mult(tmp_path, meta) -> None:
    """Production path: run_oversight_cycle_once packs action.params → spread_mult.

    Does NOT hand-pass spread_mult into apply_oversight_action (skeptic gap).
    """
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=1000.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.cfg.profiles = {"default": StrategyProfile()}
    eng.profiles[meta.condition_id] = StrategyProfile()
    fake = FakeGrok(
        oversight_payload={
            "narrative": "widen a bit",
            "reasoning": "inventory",
            "actions": [
                {
                    "type": "widen_spread",
                    "market": meta.condition_id,
                    "params": {"mult": 1.35},
                    "dry_run": False,
                    "reason": "unit",
                }
            ],
        }
    )
    eng._llm_enabled = True
    assert eng.wire_llm_stack(agent=fake, force_capital_usdc=1000.0)

    results = await eng.run_oversight_cycle_once()
    assert results, "expected at least one applied result"
    applied = [r for r in results if r.get("status") == "applied"]
    assert applied, results
    assert meta.condition_id in eng._per_market_spread_mult
    assert eng._per_market_spread_mult[meta.condition_id] == pytest.approx(1.35)
    assert applied[0].get("spread_mult") == pytest.approx(1.35)
    # Facade must have hit governance check_and_log
    assert eng._gov_facade is not None
    assert eng._gov_facade.last_decision is not None

    eng.memory.close()
    eng.state.close()
    eng.catalog.close()


@pytest.mark.asyncio
async def test_pause_market_uses_llm_paused_not_gamma_halted(tmp_path, meta) -> None:
    """pause_market must use durable _llm_paused (not Gamma _halted)."""
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=1000.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.cfg.profiles = {"default": StrategyProfile()}
    eng.profiles[meta.condition_id] = StrategyProfile()
    assert meta.condition_id not in eng._halted
    assert meta.condition_id not in eng._llm_paused

    payload = Engine.pack_oversight_action({
        "type": "pause_market",
        "market": meta.condition_id,
        "reason": "toxic",
        "dry_run": False,
    })
    result = eng.apply_oversight_action(payload)
    assert result["status"] == "applied"
    assert result.get("needs_cancel") is True
    # Durable ops pause — NOT Gamma _halted (metadata refresh discards that)
    assert meta.condition_id in eng._llm_paused
    assert meta.condition_id not in eng._halted
    assert eng.is_quoting_halted(meta.condition_id)

    eng.state.close()
    eng.catalog.close()


@pytest.mark.asyncio
async def test_pause_survives_metadata_accepting_unhalt(tmp_path, meta) -> None:
    """Simulates refresh_market_metadata accepting branch: discard _halted only.

    Production refresh does ``self._halted.discard(cid)`` for accepting markets.
    Ops pause must remain in ``_llm_paused`` so quoting stays halted.
    """
    eng = _engine_with_market(tmp_path, meta)
    eng.profiles[meta.condition_id] = StrategyProfile()
    cid = meta.condition_id

    eng.apply_oversight_action(Engine.pack_oversight_action({
        "type": "pause_market",
        "market": cid,
        "reason": "ops",
    }))
    assert cid in eng._llm_paused

    # Exactly what refresh does for acceptingOrders=True markets:
    eng._halted.discard(cid)
    # Must still be quoting-halted via _llm_paused
    assert eng.is_quoting_halted(cid)
    assert cid in eng._llm_paused

    eng.state.close()
    eng.catalog.close()


@pytest.mark.asyncio
async def test_pause_cancels_resting_orders_immediately(tmp_path, meta) -> None:
    """pause_market + flush must cancel resting quotes (not wait for recompute)."""
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=5000.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.profiles[meta.condition_id] = StrategyProfile(base_size_usdc=20.0, layers=1)
    # Low reward min so capital gate allows quotes under bankroll
    from dataclasses import replace
    eng.metas[meta.condition_id] = replace(meta, rewards_min_size=5.0, min_order_size=5.0)
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    n_before = len(eng.state.orders)
    assert n_before > 0, "expected paper quotes before pause"

    eng.apply_oversight_action(Engine.pack_oversight_action({
        "type": "pause_market",
        "market": meta.condition_id,
        "reason": "cancel_test",
    }))
    n_cancelled = await eng.flush_pause_cancels()
    assert n_cancelled == 1
    assert len(eng.state.orders) == 0
    assert eng.is_quoting_halted(meta.condition_id)

    # Further recompute must not re-place while paused
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0

    eng.state.close()
    eng.catalog.close()


@pytest.mark.asyncio
async def test_pack_and_apply_rejects_directional(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.profiles[meta.condition_id] = StrategyProfile()
    payload = Engine.pack_oversight_action({
        "type": "widen_spread",
        "market": meta.condition_id,
        "params": {"mult": 1.2},
        "side": "BUY_YES",
        "reason": "bad",
    })
    result = eng.apply_oversight_action(payload)
    assert result["status"] == "rejected_directional"
    assert meta.condition_id not in eng._per_market_spread_mult
    eng.state.close()
    eng.catalog.close()


@pytest.mark.asyncio
async def test_llm_discovery_selection_feeds_trade_list(tmp_path, meta) -> None:
    """AC1: rankings are selection input — store capital prefs / rankings."""
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=5000.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.cfg.profiles = {"default": StrategyProfile(layers=1)}
    eng.cfg.engine.auto_discovery_profile = "default"
    eng.cfg.engine.auto_discovery_max_markets = 10
    eng.profiles[meta.condition_id] = StrategyProfile(layers=1)

    # Seed catalog so top() returns our market
    eng.catalog.upsert_market(meta)

    fake = FakeGrok(
        rank_payload={
            "rankings": [
                {
                    "condition_id": meta.condition_id,
                    "confidence": 0.9,
                    "narrative": "farmable",
                    "suggested_size_pct": 0.25,
                    "risk_notes": "",
                }
            ]
        }
    )
    eng._llm_enabled = True
    assert eng.wire_llm_stack(agent=fake, force_capital_usdc=5000.0)

    rankings = await eng.run_llm_discovery_cycle_once()
    assert rankings
    assert eng._llm_rankings
    # Already in metas → capital preference updated
    assert meta.condition_id in eng._discovery_capital
    assert eng._discovery_capital[meta.condition_id] > 0

    eng.memory.close()
    eng.state.close()
    eng.catalog.close()


def test_governance_rejects_direction_on_apply_path(tmp_path) -> None:
    gov = LLMGovernance(
        capital_usdc=500.0,
        log_path=tmp_path / "llm.jsonl",
    )
    d = gov.check_and_log(
        prompt="test",
        response={"actions": {"side": "BUY_YES", "size_pct": 0.3}},
        llm_started_at=__import__("time").time(),
        confidence=0.9,
    )
    assert d.approved is False
    assert "directional" in d.rejection_reason


def test_governance_caps_size_pct(tmp_path) -> None:
    gov = LLMGovernance(capital_usdc=500.0, log_path=tmp_path / "llm.jsonl")
    d = gov.check_and_log(
        prompt="test",
        response={"actions": {"size_pct": 0.95, "spread_mult": 1.1}},
        llm_started_at=__import__("time").time(),
        confidence=1.0,
    )
    assert d.approved is True
    assert d.actions.get("size_pct", 1.0) <= 0.5


def test_pack_oversight_action_extracts_mult_from_params() -> None:
    """pack_oversight_action is the production packing used by the loop."""
    from polymaker.intelligence.oversight import OversightAction

    a = OversightAction(
        type="widen_spread",
        market="0xabc",
        params={"mult": 1.5},
        dry_run=False,
        reason="x",
    )
    p = Engine.pack_oversight_action(a)
    assert p["type"] == "widen_spread"
    assert p["condition_id"] == "0xabc"
    assert p["spread_mult"] == pytest.approx(1.5)
