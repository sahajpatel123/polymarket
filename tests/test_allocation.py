"""Tests for risk-aware capital allocation."""

from __future__ import annotations

import math

from polymaker.strategy.allocation import (
    AllocationInputs,
    allocate_capital,
    annualize_vol,
)


def test_allocate_empty_markets() -> None:
    """Empty markets list should return empty allocations."""
    inp = AllocationInputs(markets=(), total_capital_usdc=1000.0)
    out = allocate_capital(inp)
    assert out.allocations == ()
    # When markets is empty, unallocated = 0 (no capital to unallocate)
    # because the function early-returns when total_weight <= 0
    assert out.unallocated_usdc == 0.0


def test_allocate_zero_capital() -> None:
    """Zero capital should return empty allocations."""
    inp = AllocationInputs(markets=(("m1", 10.0, 0.1),), total_capital_usdc=0.0)
    out = allocate_capital(inp)
    assert out.allocations == ()


def test_allocate_skips_negative_return() -> None:
    """Markets with negative expected return should be skipped."""
    inp = AllocationInputs(
        markets=(("good", 10.0, 0.1), ("bad", -5.0, 0.1)),
        total_capital_usdc=1000.0,
    )
    out = allocate_capital(inp)
    assert len(out.allocations) == 1
    assert out.allocations[0].condition_id == "good"


def test_allocate_skips_zero_risk() -> None:
    """Markets with zero risk should be skipped (can't compute weight)."""
    inp = AllocationInputs(
        markets=(("risky", 10.0, 0.1), ("norisk", 10.0, 0.0)),
        total_capital_usdc=1000.0,
    )
    out = allocate_capital(inp)
    assert len(out.allocations) == 1
    assert out.allocations[0].condition_id == "risky"


def test_higher_return_gets_more_capital() -> None:
    """Higher expected return should get more capital allocation."""
    inp = AllocationInputs(
        markets=(
            ("low_ret", 5.0, 0.1),
            ("high_ret", 20.0, 0.1),
        ),
        total_capital_usdc=1000.0,
        max_concentration=1.0,  # no cap for this test
    )
    out = allocate_capital(inp)
    low = next(a for a in out.allocations if a.condition_id == "low_ret")
    high = next(a for a in out.allocations if a.condition_id == "high_ret")
    assert high.capital_usdc > low.capital_usdc


def test_higher_risk_gets_less_capital() -> None:
    """Higher risk should get less capital allocation (risk-parity)."""
    inp = AllocationInputs(
        markets=(
            ("low_risk", 10.0, 0.05),
            ("high_risk", 10.0, 0.20),
        ),
        total_capital_usdc=1000.0,
        max_concentration=1.0,  # no cap for this test
    )
    out = allocate_capital(inp)
    low = next(a for a in out.allocations if a.condition_id == "low_risk")
    high = next(a for a in out.allocations if a.condition_id == "high_risk")
    assert low.capital_usdc > high.capital_usdc


def test_max_concentration_cap() -> None:
    """No market should exceed the max concentration limit."""
    # One market dominates (100x return)
    inp = AllocationInputs(
        markets=(
            ("dominant", 100.0, 0.1),
            ("small", 1.0, 0.1),
        ),
        total_capital_usdc=1000.0,
        max_concentration=0.4,
    )
    out = allocate_capital(inp)
    for a in out.allocations:
        assert a.weight <= 0.4 + 0.001  # small float tolerance


def test_weights_sum_to_one() -> None:
    """Allocated weights should sum to approximately 1.0."""
    inp = AllocationInputs(
        markets=(
            ("a", 10.0, 0.1),
            ("b", 20.0, 0.2),
            ("c", 5.0, 0.05),
        ),
        total_capital_usdc=1000.0,
    )
    out = allocate_capital(inp)
    total_weight = sum(a.weight for a in out.allocations)
    assert abs(total_weight - 1.0) < 0.01


def test_min_allocation_filter() -> None:
    """Markets below min_allocation should be excluded."""
    inp = AllocationInputs(
        markets=(
            ("big", 100.0, 0.1),
            ("tiny", 0.0001, 0.1),
        ),
        total_capital_usdc=1000.0,
        min_allocation=0.05,
    )
    out = allocate_capital(inp)
    ids = [a.condition_id for a in out.allocations]
    assert "big" in ids
    assert "tiny" not in ids


def test_annualize_vol() -> None:
    """Annualization should scale by sqrt(seconds_per_year)."""
    daily_vol = 0.01
    # Per-second vol equivalent
    per_sec = daily_vol / math.sqrt(86400)
    annual = annualize_vol(per_sec)
    expected = daily_vol * math.sqrt(365.25)  # approx
    assert abs(annual - expected) < 0.001


def test_total_capital_conservation() -> None:
    """Allocated + unallocated should equal total capital."""
    inp = AllocationInputs(
        markets=(("a", 10.0, 0.1), ("b", 20.0, 0.2)),
        total_capital_usdc=1000.0,
    )
    out = allocate_capital(inp)
    assert abs(out.total_allocated_usdc + out.unallocated_usdc - 1000.0) < 0.01
