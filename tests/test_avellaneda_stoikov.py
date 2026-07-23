"""Tests for the Avellaneda-Stoikov optimal market-making model."""

from __future__ import annotations

import math

from polymaker.strategy.avellaneda_stoikov import (
    ASInputs,
    avellaneda_stoikov,
    gamma_from_profile,
    kappa_from_liquidity,
)


def test_flat_inventory_zero_skew() -> None:
    """Zero inventory should produce zero skew and reservation = mid."""
    inp = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=3600.0,
                   gamma=0.1, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert abs(out.reservation - 0.50) < 1e-9
    assert out.skew == 0.0


def test_long_inventory_lowers_reservation() -> None:
    """Long inventory should lower the reservation price (encourage selling)."""
    inp = ASInputs(mid=0.50, inventory=100.0, sigma=0.01, time_horizon_s=3600.0,
                   gamma=0.1, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert out.reservation < 0.50
    assert out.skew > 0  # positive skew (q * gamma * sigma^2 * T > 0)
    # r = s - skew, so r < s when skew > 0
    assert abs(out.reservation - (0.50 - out.skew)) < 1e-9


def test_short_inventory_raises_reservation() -> None:
    """Short inventory should raise the reservation price (encourage buying)."""
    inp = ASInputs(mid=0.50, inventory=-100.0, sigma=0.01, time_horizon_s=3600.0,
                   gamma=0.1, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert out.reservation > 0.50
    assert out.skew < 0


def test_higher_vol_widens_spread() -> None:
    """Higher volatility should produce a wider optimal spread."""
    inp_low = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=3600.0,
                       gamma=0.1, kappa=10.0)
    inp_high = ASInputs(mid=0.50, inventory=0.0, sigma=0.05, time_horizon_s=3600.0,
                        gamma=0.1, kappa=10.0)
    out_low = avellaneda_stoikov(inp_low)
    out_high = avellaneda_stoikov(inp_high)
    assert out_high.half_spread > out_low.half_spread


def test_shorter_horizon_tightens_spread() -> None:
    """Shorter time horizon should produce a tighter spread."""
    inp_long = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=7200.0,
                        gamma=0.1, kappa=10.0)
    inp_short = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=3600.0,
                         gamma=0.1, kappa=10.0)
    out_long = avellaneda_stoikov(inp_long)
    out_short = avellaneda_stoikov(inp_short)
    assert out_short.half_spread < out_long.half_spread


def test_higher_arrival_rate_tightens_spread() -> None:
    """Higher kappa (more fills expected) should tighten the spread."""
    inp_low = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=3600.0,
                       gamma=0.1, kappa=1.0)
    inp_high = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=3600.0,
                        gamma=0.1, kappa=100.0)
    out_low = avellaneda_stoikov(inp_low)
    out_high = avellaneda_stoikov(inp_high)
    assert out_high.half_spread < out_low.half_spread


def test_bid_ask_around_reservation() -> None:
    """Bid and ask should be centered on the reservation price."""
    inp = ASInputs(mid=0.50, inventory=50.0, sigma=0.01, time_horizon_s=3600.0,
                   gamma=0.1, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert abs(out.bid - (out.reservation - out.half_spread)) < 1e-9
    assert abs(out.ask - (out.reservation + out.half_spread)) < 1e-9
    assert out.bid < out.ask


def test_degenerate_inputs() -> None:
    """Degenerate inputs should return safe defaults."""
    # Zero time horizon
    inp = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=0.0,
                   gamma=0.1, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert out.half_spread == 0.0
    # Zero sigma
    inp = ASInputs(mid=0.50, inventory=0.0, sigma=0.0, time_horizon_s=3600.0,
                   gamma=0.1, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert out.half_spread == 0.0
    # Zero gamma
    inp = ASInputs(mid=0.50, inventory=0.0, sigma=0.01, time_horizon_s=3600.0,
                   gamma=0.0, kappa=10.0)
    out = avellaneda_stoikov(inp)
    assert out.half_spread == 0.0


def test_gamma_from_profile() -> None:
    """Higher risk aversion should produce a smaller gamma."""
    g_low = gamma_from_profile(0.1)
    g_high = gamma_from_profile(10.0)
    assert g_low > g_high
    assert 0.0 < g_high < 1.0


def test_kappa_from_liquidity() -> None:
    """Higher liquidity should produce a higher kappa."""
    k_low = kappa_from_liquidity(100.0)
    k_high = kappa_from_liquidity(100000.0)
    assert k_high > k_low
    assert k_low >= 1.0
    assert k_high <= 100.0


def test_optimal_spread_formula() -> None:
    """Verify the optimal spread formula matches the paper."""
    sigma = 0.01
    T = 3600.0
    gamma = 0.1
    kappa = 10.0
    inp = ASInputs(mid=0.50, inventory=0.0, sigma=sigma, time_horizon_s=T,
                   gamma=gamma, kappa=kappa)
    out = avellaneda_stoikov(inp)
    # Expected: delta = gamma * sigma^2 * T + (2/gamma) * ln(1 + gamma/kappa)
    expected_delta = gamma * sigma * sigma * T + (2.0 / gamma) * math.log(1.0 + gamma / kappa)
    expected_half = expected_delta / 2.0
    assert abs(out.half_spread - expected_half) < 1e-9
