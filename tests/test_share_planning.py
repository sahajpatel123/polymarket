"""Share-adjusted planning — dominate by book share, not monopoly fantasy.

Drives shipped APIs:
  - plan_share_adjusted / plan_capital_scenarios / rank_markets_by_share_adjusted
  - score_market (selection prefers thin-high-share over fat-low-share)
  - compute_honest_pnl share_of_pool surface
"""

from __future__ import annotations

from polymaker.catalog.scoring import score_market
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics.honest_pnl import compute_honest_pnl
from polymaker.strategy.share_planning import (
    plan_capital_scenarios,
    plan_share_adjusted,
    rank_markets_by_share_adjusted,
)


def _meta(
    *,
    cid: str,
    rewards_daily: float,
    rewards_min: float,
    liquidity: float,
    bid: float = 0.48,
    ask: float = 0.52,
) -> MarketMeta:
    return MarketMeta(
        condition_id=cid,
        question=f"Q {cid}",
        slug=cid,
        tokens=(TokenMeta(f"{cid}-y", "Yes"), TokenMeta(f"{cid}-n", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=rewards_min,
        rewards_max_spread=3.0,
        rewards_daily_rate=rewards_daily,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z",
        event_id="e1",
        best_bid=bid,
        best_ask=ask,
        liquidity_num=liquidity,
        volume_num=0.0,
        volume_24hr=0.0,
    )


# ── VP: capital scenarios ─────────────────────────────────────────────


def test_tight_bankroll_skips_high_min_size() -> None:
    plan = plan_share_adjusted(
        bankroll_usdc=30.0,
        rewards_daily_rate=300.0,
        rewards_min_size=200.0,
        market_liquidity=5000.0,
        typical_price=0.5,
        layers=2,
        condition_id="tight",
    )
    assert plan.skip is True
    assert plan.eligible is False
    assert plan.share_adjusted_expected_usdc == 0.0
    assert plan.selection_score == 0.0
    # Monopoly diagnostic may still be non-zero (ceiling only)
    assert plan.monopoly_diagnostic_usdc > 0


def test_sufficient_bankroll_floors_and_share_adjusts() -> None:
    plan = plan_share_adjusted(
        bankroll_usdc=2000.0,
        rewards_daily_rate=100.0,
        rewards_min_size=20.0,
        market_liquidity=8000.0,
        typical_price=0.5,
        layers=1,
        condition_id="ok",
    )
    assert plan.skip is False
    assert plan.eligible is True
    assert plan.quote_size_usdc > 0
    assert 0 < plan.estimated_share_of_pool <= 0.35
    assert plan.share_adjusted_expected_usdc > 0
    # Headline is strictly less than monopoly ceiling at partial share
    assert plan.share_adjusted_expected_usdc < plan.monopoly_diagnostic_usdc + 1e-9
    assert plan.selection_score == plan.share_adjusted_expected_usdc


def test_capital_scenarios_tight_vs_sufficient() -> None:
    rep = plan_capital_scenarios(
        rewards_daily_rate=200.0,
        rewards_min_size=200.0,
        market_liquidity=10000.0,
        typical_price=0.5,
        bankrolls=(30.0, 5000.0),
        condition_id="scen",
        layers=2,
    )
    assert rep.headline_kpi == "share_adjusted_expected_usdc"
    assert len(rep.scenarios) == 2
    tight, fat = rep.scenarios
    assert tight.skip is True
    assert fat.skip is False
    assert fat.share_adjusted_expected_usdc > tight.share_adjusted_expected_usdc
    d = rep.as_dict()
    assert "scenarios" in d and d["headline_kpi"] == "share_adjusted_expected_usdc"


# ── VP: thin high-share beats fat low-share ───────────────────────────


def test_rank_thin_high_share_beats_fat_low_share() -> None:
    """Same capital: thin book we can dominate > fat pool we cannot."""
    bankroll = 500.0
    markets = [
        {
            "condition_id": "fat",
            "rewards_daily_rate": 500.0,  # huge monopoly
            "rewards_min_size": 10.0,
            "liquidity_num": 500_000.0,  # dense competition
            "typical_price": 0.5,
            "n_makers": 8.0,
            "competitor_quote_usdc": 200.0,
        },
        {
            "condition_id": "thin",
            "rewards_daily_rate": 80.0,  # smaller pool
            "rewards_min_size": 10.0,
            "liquidity_num": 3_000.0,  # thin — we can take share
            "typical_price": 0.5,
            "n_makers": 2.0,
            "competitor_quote_usdc": 30.0,
        },
    ]
    ranked = rank_markets_by_share_adjusted(markets, bankroll_usdc=bankroll)
    assert ranked[0].condition_id == "thin"
    assert ranked[0].selection_score > ranked[1].selection_score
    assert ranked[0].estimated_share_of_pool > ranked[1].estimated_share_of_pool
    # Monopoly on fat is higher — prove we did NOT rank by monopoly
    assert ranked[1].monopoly_diagnostic_usdc > ranked[0].monopoly_diagnostic_usdc


def test_score_market_prefers_thin_dominable_over_fat_pool() -> None:
    """Shipped score_market entry: same bankroll, thin wins rank key."""
    bankroll = 800.0
    fat = _meta(
        cid="fat-pool",
        rewards_daily=400.0,
        rewards_min=15.0,
        liquidity=400_000.0,
    )
    thin = _meta(
        cid="thin-dom",
        rewards_daily=90.0,
        rewards_min=15.0,
        liquidity=4_000.0,
    )
    s_fat = score_market(fat, bankroll_usdc=bankroll)
    s_thin = score_market(thin, bankroll_usdc=bankroll)
    assert s_thin.score > s_fat.score
    assert s_thin.share_adjusted_expected_usdc >= s_fat.share_adjusted_expected_usdc
    assert s_thin.estimated_share_of_pool > s_fat.estimated_share_of_pool
    # Monopoly diagnostic still higher on fat — operators see ceiling vs plan
    assert s_fat.monopoly_diagnostic_usdc > s_thin.monopoly_diagnostic_usdc


def test_score_market_capital_skip_zero_score() -> None:
    m = _meta(cid="skip", rewards_daily=200.0, rewards_min=200.0, liquidity=5000.0)
    sc = score_market(m, bankroll_usdc=25.0)
    assert sc.capital_skip is True
    assert sc.score == 0.0
    assert sc.share_adjusted_expected_usdc == 0.0


# ── VP: metrics share_of_pool ──────────────────────────────────────────


def test_engine_emit_share_adjusted_planning(tmp_path, meta) -> None:
    """Shipped engine path emits share-adjusted headline + capital scenarios."""
    from polymaker.config import RiskConfig
    from tests.test_engine import _engine_with_market

    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=2000.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    from dataclasses import replace
    eng.metas[meta.condition_id] = replace(
        meta,
        rewards_min_size=10.0,
        rewards_daily_rate=100.0,
        liquidity_num=12000.0,
        best_bid=0.48,
        best_ask=0.52,
    )
    out = eng.emit_share_adjusted_planning(bankroll_usdc=2000.0, alt_bankrolls=(30.0, 2000.0))
    assert out["headline_kpi"] == "share_adjusted_expected_usdc"
    assert out["n_markets"] == 1
    m0 = out["markets"][0]
    assert "share_adjusted_expected_usdc" in m0
    assert "monopoly_diagnostic_usdc" in m0
    assert "estimated_share_of_pool" in m0
    assert len(m0["scenarios"]) == 2
    eng.state.close()
    eng.catalog.close()


def test_honest_pnl_surfaces_share_of_pool() -> None:
    h = compute_honest_pnl(
        instant_spread_usdc=1.0,
        markout_n=0,
        n_fill=20,
        n_quote=100,
        rewards_daily_rate=100.0,
        eligible_in_band_seconds=3600.0,
        monopoly_reward_usdc=50.0,
        share_adjusted_reward_usdc=3.5,
    )
    d = h.as_dict()
    assert d["headline_kpi"] == "share_adjusted_reward_usdc"
    assert d["share_adjusted_reward_usdc"] == 3.5
    assert abs(d["share_of_pool"] - 3.5 / 50.0) < 1e-9
    assert d["monopoly_reward_usdc"] == 50.0
    assert d["pnl_share_adjusted_usdc"] == h.as_adjusted_spread_usdc + 3.5
    # Monopoly alone is not the PASS path
    assert "pnl_monopoly_diagnostic_usdc" in d
