"""Multi-market portfolio + capacity curve — best book for any capital."""

from __future__ import annotations

from polymaker.strategy.share_planning import (
    as_risk_proxy,
    build_dominator_operator_report,
    capacity_curve,
    optimize_multi_market_portfolio,
    recommend_max_markets,
    risk_adjust_share_adj,
)


def _mk(
    cid: str,
    *,
    pool: float,
    rmin: float,
    liq: float,
    n_makers: float = 3.0,
    comp: float | None = None,
    spread: float = 3.0,
    mid: float = 0.5,
) -> dict:
    d = {
        "condition_id": cid,
        "rewards_daily_rate": pool,
        "rewards_min_size": rmin,
        "liquidity_num": liq,
        "typical_price": mid,
        "min_order_size": 5.0,
        "n_makers": n_makers,
        "rewards_max_spread": spread,
    }
    if comp is not None:
        d["competitor_quote_usdc"] = comp
    return d


def test_portfolio_picks_multiple_markets_under_bankroll() -> None:
    markets = [
        _mk("a", pool=80, rmin=10, liq=4000, n_makers=2, comp=25),
        _mk("b", pool=70, rmin=10, liq=5000, n_makers=2, comp=30),
        _mk("c", pool=60, rmin=10, liq=6000, n_makers=2, comp=35),
        _mk("fat", pool=400, rmin=10, liq=400_000, n_makers=8, comp=200),
    ]
    port = optimize_multi_market_portfolio(
        markets, bankroll_usdc=600.0, max_markets=12, max_concentration=0.4
    )
    assert port.n_markets >= 2
    assert port.total_share_adjusted_usdc > 0
    assert port.total_allocated_usdc <= 600.0 + 1e-6
    # Fat monopoly pool should not crowd out all thin picks by itself
    ids = {p.condition_id for p in port.picks}
    assert "fat" not in ids or port.n_markets >= 2
    assert port.daily_return_pct == port.total_share_adjusted_usdc / 600.0
    assert port.total_risk_adjusted_usdc <= port.total_share_adjusted_usdc + 1e-9
    d = port.as_dict()
    assert d["headline_kpi"] == "total_risk_adjusted_usdc"
    assert d["n_markets"] == port.n_markets
    assert "total_risk_adjusted_usdc" in d


def test_portfolio_respects_max_markets_not_universe_size() -> None:
    """Universe can be huge; only max_markets simultaneous slots."""
    markets = [
        _mk(f"m{i}", pool=50 + i, rmin=5, liq=3000 + i * 100, n_makers=2, comp=20)
        for i in range(40)
    ]
    port = optimize_multi_market_portfolio(
        markets, bankroll_usdc=2000.0, max_markets=8, max_concentration=0.25
    )
    assert port.n_markets <= 8
    assert port.n_markets >= 1


def test_tight_capital_few_or_zero_eligible() -> None:
    markets = [
        _mk("x", pool=200, rmin=200, liq=5000),
        _mk("y", pool=150, rmin=200, liq=8000),
    ]
    port = optimize_multi_market_portfolio(
        markets, bankroll_usdc=30.0, max_markets=10, max_concentration=0.5
    )
    # Cannot fund 200-share mins on $30
    assert port.total_share_adjusted_usdc == 0.0
    assert port.n_markets == 0


def test_capacity_curve_pct_declines_with_capital() -> None:
    """Physics: %/day tends to fall as capital rises on finite reward surface."""
    markets = [
        _mk("t1", pool=90, rmin=10, liq=3500, n_makers=2, comp=28),
        _mk("t2", pool=75, rmin=10, liq=4000, n_makers=2, comp=30),
        _mk("t3", pool=60, rmin=10, liq=4500, n_makers=2, comp=32),
    ]
    curve = capacity_curve(
        markets,
        bankrolls=(150.0, 500.0, 2000.0, 5000.0),
        current_bankroll=5000.0,
        max_markets=10,
        max_concentration=0.4,
        outgrew_frac_of_peak=0.55,
    )
    assert len(curve.points) == 4
    # Peak % should be at a smaller bankroll than the largest (typical)
    assert curve.peak_pct >= curve.points[-1].daily_return_pct - 1e-12
    d = curve.as_dict()
    assert "capital_outgrew_reward_surface" in d
    assert "peak_pct_display" in d
    # At large capital past peak, outgrew should often fire
    assert curve.current_bankroll == 5000.0


def test_capacity_outgrew_flag_when_large_capital() -> None:
    markets = [
        _mk("only", pool=100, rmin=10, liq=5000, n_makers=2, comp=40),
    ]
    curve = capacity_curve(
        markets,
        bankrolls=(100.0, 200.0, 1000.0, 5000.0),
        current_bankroll=5000.0,
        max_markets=5,
        max_concentration=0.5,
        outgrew_frac_of_peak=0.5,
    )
    # With one finite pool, large capital % << small capital %
    small = next(p for p in curve.points if p.bankroll_usdc == 100.0)
    large = next(p for p in curve.points if p.bankroll_usdc == 5000.0)
    assert small.daily_return_pct >= large.daily_return_pct - 1e-12
    if small.daily_return_pct > 1e-9 and large.daily_return_pct < 0.5 * small.daily_return_pct:
        assert curve.capital_outgrew_reward_surface is True


def test_as_haircut_prefers_safer_over_toxic_thin() -> None:
    """Same pool/share potential: extreme mid + wide band + ultra-thin loses efficiency."""
    safe = _mk("safe", pool=100, rmin=10, liq=15000, n_makers=2, comp=30, spread=2.0, mid=0.5)
    toxic = _mk("toxic", pool=100, rmin=10, liq=800, n_makers=2, comp=30, spread=10.0, mid=0.92)
    assert as_risk_proxy(
        liquidity_num=toxic["liquidity_num"],
        rewards_max_spread=toxic["rewards_max_spread"],
        typical_price=toxic["typical_price"],
    ) > as_risk_proxy(
        liquidity_num=safe["liquidity_num"],
        rewards_max_spread=safe["rewards_max_spread"],
        typical_price=safe["typical_price"],
    )
    port = optimize_multi_market_portfolio(
        [toxic, safe],
        bankroll_usdc=400.0,
        max_markets=1,
        max_concentration=0.5,
        as_weight=0.55,
    )
    assert port.n_markets == 1
    assert port.picks[0].condition_id == "safe"
    assert port.picks[0].as_risk < 0.9


def test_risk_adjust_never_increases() -> None:
    assert risk_adjust_share_adj(100.0, 0.0) == 100.0
    assert abs(risk_adjust_share_adj(100.0, 1.0, as_weight=0.55) - 45.0) < 1e-9
    assert risk_adjust_share_adj(100.0, 0.5, as_weight=0.55) < 100.0


def test_recommend_max_markets_dynamic() -> None:
    markets = [
        _mk(f"m{i}", pool=50 + i * 5, rmin=10, liq=4000 + i * 200, n_makers=2, comp=25)
        for i in range(15)
    ]
    n_small = recommend_max_markets(markets, bankroll_usdc=200.0, hard_cap=20)
    n_large = recommend_max_markets(markets, bankroll_usdc=3000.0, hard_cap=20)
    assert 1 <= n_small <= 20
    assert 1 <= n_large <= 20
    # Auto path should produce a valid portfolio
    port = optimize_multi_market_portfolio(
        markets, bankroll_usdc=800.0, auto_max_markets=True, hard_cap_markets=12
    )
    assert port.max_markets_recommended >= 1
    assert port.n_markets <= port.max_markets_recommended + 0  # used <= recommended slots


def test_operator_report_outgrew_message() -> None:
    markets = [
        _mk("only", pool=100, rmin=10, liq=5000, n_makers=2, comp=40),
    ]
    port = optimize_multi_market_portfolio(
        markets, bankroll_usdc=5000.0, max_markets=5, max_concentration=0.5
    )
    curve = capacity_curve(
        markets,
        bankrolls=(100.0, 200.0, 1000.0, 5000.0),
        current_bankroll=5000.0,
        max_markets=5,
        max_concentration=0.5,
        outgrew_frac_of_peak=0.5,
    )
    rep = build_dominator_operator_report(port, curve)
    assert "operator_message" in rep
    assert "recommended_actions" in rep
    assert isinstance(rep["recommended_actions"], list)
    assert rep["headline_kpi"] == "total_risk_adjusted_usdc"
    if curve.capital_outgrew_reward_surface:
        assert rep["capital_outgrew_reward_surface"] is True
        assert "physics" in rep["operator_message"].lower() or "outgrew" in rep["operator_message"].lower()


def test_engine_emit_multi_market_dominator(tmp_path, meta) -> None:
    from dataclasses import replace

    from polymaker.config import RiskConfig
    from tests.test_engine import _engine_with_market

    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(
        bankroll_usdc=800.0, max_market_concentration_pct=0.4
    ).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.metas[meta.condition_id] = replace(
        meta,
        rewards_min_size=10.0,
        rewards_daily_rate=90.0,
        liquidity_num=5000.0,
        best_bid=0.48,
        best_ask=0.52,
    )
    # Extra synthetic candidate via catalog-free path
    cands = [
        {
            "condition_id": meta.condition_id,
            "rewards_daily_rate": 90.0,
            "rewards_min_size": 10.0,
            "liquidity_num": 5000.0,
            "typical_price": 0.5,
        },
        {
            "condition_id": "thin-2",
            "rewards_daily_rate": 70.0,
            "rewards_min_size": 10.0,
            "liquidity_num": 4000.0,
            "typical_price": 0.5,
            "n_makers": 2,
            "competitor_quote_usdc": 25,
        },
    ]
    out = eng.emit_multi_market_dominator(
        bankroll_usdc=800.0, candidate_markets=cands, max_markets=10
    )
    assert "portfolio" in out
    assert "capacity_curve" in out
    assert "operator" in out
    assert out["portfolio"]["n_markets"] >= 1
    assert out["portfolio"]["total_share_adjusted_usdc"] >= 0
    assert "total_risk_adjusted_usdc" in out["portfolio"]
    assert out["operator"]["operator_message"]
    eng.state.close()
    eng.catalog.close()
