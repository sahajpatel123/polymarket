"""Tests for V3 single-capital orchestrator.

The orchestrator is the *only* place that decides "how much capital
goes where". These tests verify the greedy fill algorithm, the
per-market cap, the reward floor, the max-markets limit, and the
skipped-reason accounting.
"""

from __future__ import annotations

import pytest

from polymaker.intelligence.orchestrator import (
    MarketCandidate,
    plan_allocations,
)
from polymaker.intelligence.policy import RiskPolicy

# ── Helpers ──────────────────────────────────────────────────────────


def _policy(capital: float = 1000.0, **overrides) -> ...:
    """Build a ResolvedPolicy with predictable defaults."""
    p = RiskPolicy(
        max_per_market_pct=overrides.get("max_per_market_pct", 0.05),
        total_exposure_pct=overrides.get("total_exposure_pct", 1.0),
        daily_loss_kill_pct=overrides.get("daily_loss_kill_pct", 0.10),
        max_drawdown_kill_pct=overrides.get("max_drawdown_kill_pct", 0.25),
        per_market_loss_pct=overrides.get("per_market_loss_pct", 0.05),
        per_trade_loss_pct=overrides.get("per_trade_loss_pct", 0.005),
        target_daily_growth_pct=overrides.get("target_daily_growth_pct", 0.10),
        min_reward_pct_per_day=overrides.get("min_reward_pct_per_day", 0.005),
        max_concurrent_markets=overrides.get("max_concurrent_markets", 8),
    )
    return p.resolve(capital_usdc=capital)


def _candidate(
    cid: str = "0xa",
    confidence: float = 0.7,
    reward_per_day: float = 10.0,
    **overrides,
) -> MarketCandidate:
    return MarketCandidate(
        condition_id=cid,
        slug=overrides.get("slug", cid),
        confidence=confidence,
        expected_reward_per_day_usdc=reward_per_day,
        exchange_min_shares=overrides.get("exchange_min_shares", 5.0),
        reward_min_shares=overrides.get("reward_min_shares", 200.0),
        typical_price=overrides.get("typical_price", 0.5),
        category=overrides.get("category", ""),
        narrative=overrides.get("narrative", ""),
        suggested_size_pct=overrides.get("suggested_size_pct"),
    )


# ── Basic happy path ─────────────────────────────────────────────────


def test_plan_empty_candidates():
    p = _policy()
    plan = plan_allocations([], p)
    assert plan.allocations == ()
    assert plan.skipped == ()
    assert plan.total_allocated_usdc == 0.0
    assert plan.unallocated_usdc == p.total_exposure_usdc


def test_plan_zero_capital_returns_empty():
    p = RiskPolicy().resolve(0)
    c = _candidate(confidence=0.9, reward_per_day=20.0)
    plan = plan_allocations([c], p)
    assert plan.allocations == ()
    assert plan.total_allocated_usdc == 0.0
    assert plan.unallocated_usdc == 0.0


def test_plan_single_high_confidence_market():
    p = _policy(capital=1000.0)
    c = _candidate(cid="0x1", confidence=0.9, reward_per_day=20.0)
    plan = plan_allocations([c], p)
    assert len(plan.allocations) == 1
    assert plan.allocations[0].condition_id == "0x1"
    assert plan.allocations[0].allocation_usdc > 0
    # Capped at 5% of capital = $50.
    assert plan.allocations[0].allocation_usdc <= 50.0


def test_plan_respects_per_market_cap():
    """Even a high-confidence, high-reward market can't exceed 5%."""
    p = _policy(capital=1000.0, max_per_market_pct=0.05)
    c = _candidate(confidence=1.0, reward_per_day=1000.0)
    plan = plan_allocations([c], p)
    assert plan.allocations[0].allocation_usdc == pytest.approx(50.0, abs=1e-6)


def test_plan_sorts_by_score_descending():
    """Highest score first."""
    p = _policy(capital=1000.0, max_per_market_pct=0.20)
    candidates = [
        _candidate(cid="0xlow", confidence=0.3, reward_per_day=5.0),
        _candidate(cid="0xhi", confidence=0.9, reward_per_day=20.0),
        _candidate(cid="0xmid", confidence=0.6, reward_per_day=15.0),
    ]
    plan = plan_allocations(candidates, p)
    ids = [a.condition_id for a in plan.allocations]
    assert ids[0] == "0xhi"
    assert "0xmid" in ids or "0xlow" in ids  # mid may be cut by budget


def test_plan_runs_out_of_budget_skips_remainder():
    """Once total budget is consumed, rest go to skipped."""
    p = _policy(
        capital=100.0, max_per_market_pct=0.50, max_concurrent_markets=10,
    )
    # 5 markets each wanting 50% = $50. Total = $250. Budget = $100.
    candidates = [
        _candidate(cid=f"0x{i}", confidence=0.9, reward_per_day=20.0)
        for i in range(5)
    ]
    plan = plan_allocations(candidates, p)
    # Only 2 should fit (2 × $50 = $100).
    assert len(plan.allocations) == 2
    assert plan.total_allocated_usdc <= 100.0
    # 3 should be skipped with budget_exhausted.
    reasons = {s.skipped_reason for s in plan.skipped}
    assert "budget_exhausted" in reasons


# ── Filtering ────────────────────────────────────────────────────────


def test_plan_skips_zero_confidence():
    p = _policy()
    c = _candidate(confidence=0.0, reward_per_day=10.0)
    plan = plan_allocations([c], p)
    assert plan.allocations == ()
    assert len(plan.skipped) == 1
    assert plan.skipped[0].skipped_reason == "zero_confidence_or_reward"


def test_plan_skips_zero_reward():
    p = _policy()
    c = _candidate(confidence=0.5, reward_per_day=0.0)
    plan = plan_allocations([c], p)
    assert plan.allocations == ()
    assert plan.skipped[0].skipped_reason == "zero_confidence_or_reward"


def test_plan_skips_below_reward_floor():
    p = _policy(capital=1000.0, min_reward_pct_per_day=0.01)
    # 0.5% of $1000 = $5, but the floor is 1% = $10.
    c = _candidate(confidence=0.9, reward_per_day=5.0)
    plan = plan_allocations([c], p)
    assert plan.allocations == ()
    assert plan.skipped[0].skipped_reason == "below_reward_floor"


def test_plan_max_markets_caps_count():
    p = _policy(capital=10000.0, max_per_market_pct=0.01, max_concurrent_markets=3)
    # Need reward >= 0.5% of capital per day = $50.
    candidates = [
        _candidate(cid=f"0x{i}", confidence=0.9, reward_per_day=60.0)
        for i in range(10)
    ]
    plan = plan_allocations(candidates, p)
    assert len(plan.allocations) == 3
    skipped_reasons = [s.skipped_reason for s in plan.skipped]
    assert "max_markets_reached" in skipped_reasons


def test_plan_min_viable_size_zeros_tiny():
    p = _policy(capital=10.0, max_per_market_pct=0.01)
    # $10 × 1% = $0.10, below $5 min viable.
    c = _candidate(confidence=0.5, reward_per_day=0.06)  # 0.6% per day
    plan = plan_allocations([c], p, min_viable_allocation_usdc=5.0)
    assert plan.allocations == ()
    assert plan.skipped[0].skipped_reason == "below_min_viable_size"


# ── LLM suggested_size_pct ──────────────────────────────────────────


def test_plan_llm_size_pct_caps_allocation():
    """If LLM says 'use at most 2%', that should override the policy cap."""
    p = _policy(capital=1000.0, max_per_market_pct=0.10)
    c = _candidate(confidence=1.0, reward_per_day=100.0, suggested_size_pct=0.02)
    plan = plan_allocations([c], p)
    # 2% of $1000 = $20, vs policy cap of 10% = $100. LLM wins.
    assert plan.allocations[0].allocation_usdc <= 20.0


def test_plan_suggested_size_pct_zero_means_no_override():
    """A 0 or None suggested_size_pct should NOT constrain."""
    p = _policy(capital=1000.0, max_per_market_pct=0.10)
    c = _candidate(confidence=1.0, reward_per_day=100.0, suggested_size_pct=None)
    plan = plan_allocations([c], p)
    assert plan.allocations[0].allocation_usdc == pytest.approx(100.0, abs=1e-6)


# ── AllocationPlan helpers ──────────────────────────────────────────


def test_plan_active_markets_returns_cids():
    p = _policy()
    c1 = _candidate(cid="0xa", confidence=0.9, reward_per_day=20.0)
    c2 = _candidate(cid="0xb", confidence=0.8, reward_per_day=15.0)
    plan = plan_allocations([c1, c2], p)
    active = plan.active_markets()
    assert "0xa" in active
    assert "0xb" in active


def test_plan_for_market_returns_allocation():
    p = _policy()
    c = _candidate(cid="0x1", confidence=0.9, reward_per_day=20.0)
    plan = plan_allocations([c], p)
    found = plan.for_market("0x1")
    assert found is not None
    assert found.allocation_usdc > 0
    assert plan.for_market("0xnothere") is None


def test_plan_unallocated_usdc_equals_budget_minus_allocated():
    p = _policy(capital=1000.0, max_per_market_pct=0.05, total_exposure_pct=1.0)
    c = _candidate(confidence=0.9, reward_per_day=20.0)
    plan = plan_allocations([c], p)
    assert plan.unallocated_usdc == pytest.approx(
        p.total_exposure_usdc - plan.total_allocated_usdc, abs=1e-6
    )


def test_plan_assigns_sequential_ranks():
    p = _policy(capital=1000.0, max_per_market_pct=0.20)
    candidates = [
        _candidate(cid=f"0x{i}", confidence=0.9, reward_per_day=20.0)
        for i in range(3)
    ]
    plan = plan_allocations(candidates, p)
    ranks = [a.rank for a in plan.allocations]
    # Ranks start at 1 and are sequential.
    assert ranks == sorted(ranks)
    assert ranks[0] == 1
