"""Tests for the Kelly-inspired position sizing model."""

from __future__ import annotations

from polymaker.strategy.kelly import (
    KellyInputs,
    edge_from_spread,
    kelly_size,
    time_horizon_from_liquidity,
)


def test_positive_edge_produces_positive_size() -> None:
    """Positive edge should produce a positive size (buy signal)."""
    inp = KellyInputs(
        edge=0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    out = kelly_size(inp)
    assert out.size_shares > 0
    assert out.fraction > 0


def test_negative_edge_produces_negative_size() -> None:
    """Negative edge should produce a negative size (sell signal)."""
    inp = KellyInputs(
        edge=-0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    out = kelly_size(inp)
    assert out.size_shares < 0
    assert out.fraction < 0


def test_higher_edge_larger_size() -> None:
    """Higher edge should produce a larger size."""
    inp_low = KellyInputs(
        edge=0.005, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    inp_high = KellyInputs(
        edge=0.02, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    out_low = kelly_size(inp_low)
    out_high = kelly_size(inp_high)
    assert out_high.size_shares > out_low.size_shares


def test_higher_vol_smaller_size() -> None:
    """Higher volatility should produce a smaller size (more risk)."""
    inp_low = KellyInputs(
        edge=0.01, sigma=0.005, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    inp_high = KellyInputs(
        edge=0.01, sigma=0.05, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    out_low = kelly_size(inp_low)
    out_high = kelly_size(inp_high)
    assert out_high.size_shares < out_low.size_shares


def test_existing_inventory_reduces_size() -> None:
    """Existing inventory in the same direction should reduce the size."""
    inp_flat = KellyInputs(
        edge=0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    inp_long = KellyInputs(
        edge=0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=200.0,  # already long
        max_inventory_shares=500.0, price=0.50,
    )
    out_flat = kelly_size(inp_flat)
    out_long = kelly_size(inp_long)
    assert out_long.size_shares < out_flat.size_shares


def test_max_inventory_cap() -> None:
    """Size should be capped by the maximum inventory limit."""
    inp = KellyInputs(
        edge=0.10, sigma=0.001, time_horizon_s=10.0,  # huge edge, tiny vol
        bankroll_usdc=1000000.0, inventory_shares=0.0,
        max_inventory_shares=100.0, price=0.50,
    )
    out = kelly_size(inp)
    assert abs(out.size_shares) <= 100.0


def test_min_size_threshold() -> None:
    """Size below the minimum threshold should be zeroed."""
    inp = KellyInputs(
        edge=0.0001, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=10.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
        min_size_shares=5.0,
    )
    out = kelly_size(inp)
    assert out.size_shares == 0.0


def test_zero_bankroll() -> None:
    """Zero bankroll should produce zero size."""
    inp = KellyInputs(
        edge=0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=0.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
    )
    out = kelly_size(inp)
    assert out.size_shares == 0.0


def test_kelly_fraction_scales_size() -> None:
    """Higher Kelly fraction should produce a larger size."""
    inp_quarter = KellyInputs(
        edge=0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
        kelly_fraction=0.25,
    )
    inp_half = KellyInputs(
        edge=0.01, sigma=0.01, time_horizon_s=3600.0,
        bankroll_usdc=1000.0, inventory_shares=0.0,
        max_inventory_shares=500.0, price=0.50,
        kelly_fraction=0.5,
    )
    out_quarter = kelly_size(inp_quarter)
    out_half = kelly_size(inp_half)
    assert out_half.size_shares > out_quarter.size_shares


def test_edge_from_spread() -> None:
    """Edge from spread should be at least the tick size."""
    assert edge_from_spread(0.01, 0.001) == 0.01
    assert edge_from_spread(0.0001, 0.001) == 0.001  # floored to tick


def test_time_horizon_from_liquidity() -> None:
    """Higher liquidity should produce a shorter time horizon."""
    t_thin = time_horizon_from_liquidity(100.0)
    t_deep = time_horizon_from_liquidity(100000.0)
    assert t_deep < t_thin
    assert t_deep >= 60.0
    assert t_thin <= 3600.0
