"""Capital-aware reward eligibility: floor size or skip (shipped path).

Drives :func:`decide_maker_reward_eligibility` and
:meth:`RiskConfig.scale_profile_sizes` — the same APIs the engine uses.
"""

from __future__ import annotations

import asyncio

import pytest

from polymaker.benchmark.capital import (
    decide_maker_reward_eligibility,
)
from polymaker.config import RiskConfig, StrategyProfile
from polymaker.domain import Side
from polymaker.engine import Engine
from polymaker.strategy.regime import RegimeMachine
from tests.test_engine import _engine_with_market, _feed_book


# ── Pure gate: undersized skip vs sufficient qualify ──────────────────


def test_undersized_capital_skips_reward_market() -> None:
    """$30 bankroll cannot fund 200-share two-sided at $0.50 → skip."""
    gate = decide_maker_reward_eligibility(
        bankroll_usdc=30.0,
        rewards_min_size=200.0,
        exchange_min_shares=5.0,
        typical_price=0.50,
        layers=2,
    )
    assert gate.skip is True
    assert gate.eligible is False
    assert "INSUFFICIENT" in gate.reason
    assert gate.recommended_base_size_usdc == 0.0
    # min notional alone is 100 USDC; two-sided * layers is larger
    assert gate.min_order_notional_usdc >= 100.0 - 1e-6
    assert gate.required_two_sided_usdc > gate.bankroll_usdc * 0.5


def test_sufficient_capital_qualifies_and_floors_size() -> None:
    """$2000 bankroll can fund 200-share two-sided → eligible + floor base."""
    gate = decide_maker_reward_eligibility(
        bankroll_usdc=2000.0,
        rewards_min_size=200.0,
        exchange_min_shares=5.0,
        typical_price=0.50,
        layers=1,
        reward_size_mult=1.0,
    )
    assert gate.skip is False
    assert gate.eligible is True
    assert gate.reason == "reward_eligible"
    # Floor at least 200 * 0.50 = 100 USDC notional
    assert gate.recommended_base_size_usdc >= 100.0 - 1e-6
    assert gate.min_shares >= 200.0 - 1e-6


def test_bankroll_unset_does_not_skip() -> None:
    """Legacy / no-bankroll configs must not block quoting."""
    gate = decide_maker_reward_eligibility(
        bankroll_usdc=0.0,
        rewards_min_size=200.0,
        typical_price=0.5,
    )
    assert gate.skip is False
    assert gate.reason == "bankroll_unset"


def test_no_reward_min_still_quotes() -> None:
    gate = decide_maker_reward_eligibility(
        bankroll_usdc=100.0,
        rewards_min_size=0.0,
        exchange_min_shares=5.0,
        typical_price=0.5,
        layers=1,
    )
    assert gate.skip is False
    assert gate.eligible is True
    assert gate.recommended_base_size_usdc > 0


def test_tiny_bankroll_vs_high_min_shares_skips() -> None:
    """Even one side of reward min exceeds usable capital → skip."""
    gate = decide_maker_reward_eligibility(
        bankroll_usdc=10.0,
        rewards_min_size=50.0,
        exchange_min_shares=5.0,
        typical_price=0.80,
        layers=1,
        safety_frac=0.5,
    )
    # min notional = 40; usable = 5 → insufficient
    assert gate.skip is True
    assert "INSUFFICIENT" in gate.reason


def test_scale_profile_sizes_floors_when_eligible() -> None:
    cfg = RiskConfig(
        bankroll_usdc=2000.0,
        market_notional_frac=0.35,
    ).resolve_from_bankroll()
    p = StrategyProfile(base_size_usdc=3.0, q_max_usdc=30.0, layers=1)
    scaled = cfg.scale_profile_sizes(
        p,
        rewards_min_size=200.0,
        typical_price=0.50,
        exchange_min_shares=5.0,
    )
    assert scaled.base_size_usdc >= 100.0 - 1e-6
    assert scaled.bankroll_usdc == 2000.0


def test_scale_profile_sizes_no_silent_floor_when_unaffordable() -> None:
    """When skip would apply, scale_profile_sizes does not invent a tiny floor."""
    cfg = RiskConfig(
        bankroll_usdc=30.0,
        market_notional_frac=0.4,
    ).resolve_from_bankroll()
    p = StrategyProfile(base_size_usdc=3.0, layers=2)
    scaled = cfg.scale_profile_sizes(
        p,
        rewards_min_size=200.0,
        typical_price=0.50,
    )
    # Must not pretend we can quote 100 USDC when bankroll is 30
    assert scaled.base_size_usdc < 100.0
    gate = decide_maker_reward_eligibility(
        bankroll_usdc=30.0,
        rewards_min_size=200.0,
        typical_price=0.50,
        layers=2,
    )
    assert gate.skip is True


# ── Engine path: skip cancels; qualify places with floor ───────────────


def _set_bankroll(eng: Engine, bankroll: float) -> None:
    eng.cfg.risk = eng.cfg.risk.model_copy(update={"bankroll_usdc": bankroll}).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk


def _replace_meta(meta, **kwargs):
    fields = {f.name: getattr(meta, f.name) for f in meta.__dataclass_fields__.values()}
    fields.update(kwargs)
    return type(meta)(**fields)


async def test_engine_skips_when_capital_insufficient(tmp_path, meta) -> None:
    """With tiny bankroll + high rewards_min, recompute must not place quotes."""
    eng = _engine_with_market(tmp_path, meta)
    _set_bankroll(eng, 25.0)
    # Force a reward min the bankroll cannot fund two-sided
    eng.metas[meta.condition_id] = _replace_meta(
        meta, rewards_min_size=200.0, min_order_size=5.0
    )
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0
    gate = eng._reward_eligibility.get(meta.condition_id)
    assert gate is not None
    assert gate.skip is True
    assert "INSUFFICIENT" in gate.reason
    eng.state.close()
    eng.catalog.close()


async def test_engine_qualifies_and_places_when_capital_sufficient(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    _set_bankroll(eng, 5000.0)
    eng.metas[meta.condition_id] = _replace_meta(
        meta, rewards_min_size=20.0, min_order_size=5.0
    )
    eng.profiles[meta.condition_id] = StrategyProfile(
        base_size_usdc=5.0,
        layers=1,
        reward_size_mult=1.0,
    )
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    gate = eng._reward_eligibility.get(meta.condition_id)
    assert gate is not None
    assert gate.skip is False
    assert gate.eligible is True
    yes_orders = eng.state.orders_for(meta.yes.token_id)
    no_orders = eng.state.orders_for(meta.no.token_id)
    assert yes_orders or no_orders, "expected quotes when capital qualifies"
    eng.state.close()
    eng.catalog.close()
