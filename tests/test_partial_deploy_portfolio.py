"""Partial deploy multi-market: small simultaneous slices, reserve capital."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymaker.strategy.share_planning import (
    _horizon_boost,
    optimize_multi_market_portfolio,
)


def _mk(cid: str, *, pool: float, rmin: float, liq: float, days: float | None = 7.0) -> dict:
    d: dict = {
        "condition_id": cid,
        "rewards_daily_rate": pool,
        "rewards_min_size": rmin,
        "liquidity_num": liq,
        "typical_price": 0.5,
        "min_order_size": 5.0,
        "n_makers": 2,
        "competitor_quote_usdc": 25,
        "rewards_max_spread": 3.0,
    }
    if days is not None:
        end = datetime.now(timezone.utc) + timedelta(days=days)
        d["end_date_iso"] = end.isoformat().replace("+00:00", "Z")
    return d


def test_partial_deploy_keeps_reserve_at_50() -> None:
    """$50 bankroll: deploy 60% → working $30, reserve $20; never full dump."""
    markets = [
        _mk("a", pool=80, rmin=5, liq=5000, days=5),
        _mk("b", pool=70, rmin=5, liq=6000, days=8),
        _mk("c", pool=60, rmin=5, liq=7000, days=10),
        _mk("far", pool=200, rmin=5, liq=8000, days=90),
    ]
    port = optimize_multi_market_portfolio(
        markets,
        bankroll_usdc=50.0,
        max_markets=5,
        max_concentration=0.25,  # ≤$12.5 / market
        capital_deploy_frac=0.60,  # $30 working
        prefer_horizon_days=14.0,
    )
    assert port.capital_deploy_frac == 0.60
    assert abs(port.working_capital_usdc - 30.0) < 1e-6
    assert abs(port.reserve_usdc - 20.0) < 1e-6
    # Live allocated cannot exceed working capital
    assert port.total_allocated_usdc <= 30.0 + 1e-6
    # Must leave reserve: allocated + unallocated == bankroll
    assert abs(port.total_allocated_usdc + port.unallocated_usdc - 50.0) < 1e-3
    assert port.reserve_usdc > 0
    # Not one market taking full $50
    for p in port.picks:
        assert p.allocated_usdc <= 50.0 * 0.25 + 1e-6
    d = port.as_dict()
    assert d["policy"] == "partial_deploy_multi_market"


def test_partial_deploy_multiple_markets_when_eligible() -> None:
    markets = [
        _mk(f"m{i}", pool=50 + i * 10, rmin=5, liq=4000 + i * 500, days=3 + i)
        for i in range(6)
    ]
    port = optimize_multi_market_portfolio(
        markets,
        bankroll_usdc=50.0,
        max_markets=4,
        max_concentration=0.25,
        capital_deploy_frac=0.60,
        prefer_horizon_days=14.0,
    )
    # With low mins and small slices, should open more than 1 market when possible
    assert port.n_markets >= 2
    assert port.total_allocated_usdc < 50.0  # never full wallet live


def test_horizon_prefers_two_week_events() -> None:
    near = _mk("near", pool=100, rmin=5, liq=5000, days=5)
    far = _mk("far", pool=100, rmin=5, liq=5000, days=120)
    assert _horizon_boost(near, prefer_horizon_days=14.0) > _horizon_boost(
        far, prefer_horizon_days=14.0
    )
    port = optimize_multi_market_portfolio(
        [far, near],
        bankroll_usdc=50.0,
        max_markets=1,
        max_concentration=0.25,
        capital_deploy_frac=0.60,
        prefer_horizon_days=14.0,
    )
    assert port.n_markets == 1
    assert port.picks[0].condition_id == "near"


def test_full_deploy_still_available_for_legacy() -> None:
    markets = [_mk("x", pool=80, rmin=5, liq=5000, days=7)]
    port = optimize_multi_market_portfolio(
        markets,
        bankroll_usdc=50.0,
        max_markets=3,
        max_concentration=1.0,
        capital_deploy_frac=1.0,
        prefer_horizon_days=0.0,
    )
    assert port.reserve_usdc == 0.0
    assert port.working_capital_usdc == 50.0
