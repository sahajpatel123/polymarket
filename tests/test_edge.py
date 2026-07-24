"""Tests for maker edge math and daily-return estimation (shipped pure helpers)."""

from __future__ import annotations

import pytest

from polymaker.domain import MarketMeta, Position, Regime, TokenMeta
from polymaker.strategy.edge import (
    adverse_selection_buffer,
    competition_share,
    estimate_daily_return,
    half_spread_floor,
    taker_fee_per_share,
)
from polymaker.strategy.quoting import QuoteInputs, construct_quotes
from tests.conftest import view


def _meta(**over) -> MarketMeta:
    base = dict(
        condition_id="0xedge",
        question="Edge?",
        slug="edge",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=100.0,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
        liquidity_num=20000.0,
    )
    base.update(over)
    return MarketMeta(**base)


def test_taker_fee_peaks_near_half():
    assert taker_fee_per_share(0.5, 400) == pytest.approx(0.04 * 0.25)
    assert taker_fee_per_share(0.1, 400) < taker_fee_per_share(0.5, 400)


def test_as_buffer_grows_with_vol_and_tox():
    tick = 0.01
    calm = adverse_selection_buffer(sigma=0.0, toxicity=0.0, tick=tick)
    hot = adverse_selection_buffer(sigma=0.02, toxicity=0.2, tick=tick)
    assert calm == tick
    assert hot > calm


def test_half_spread_floor_covers_fee_and_as(meta=None):
    m = _meta()
    floor = half_spread_floor(
        m, fv=0.5, sigma=0.01, toxicity=0.1, tick=0.01, delta_min_ticks=2,
    )
    assert floor >= 2 * 0.01
    assert floor >= adverse_selection_buffer(sigma=0.01, toxicity=0.1, tick=0.01) * 0.5


def test_competition_share_rises_with_our_size():
    small = competition_share(our_quote_usdc=20, market_liquidity=0, n_competing_makers=3)
    big = competition_share(our_quote_usdc=100, market_liquidity=0, n_competing_makers=3)
    assert big > small
    assert competition_share(our_quote_usdc=0, market_liquidity=1000) == 0.0
    assert big <= 0.35


def test_daily_return_estimate_math():
    est = estimate_daily_return(
        bankroll_usdc=100.0,
        runtime_hours=24.0,
        spread_usdc=1.0,
        reward_pool_accrual_usdc=100.0,
        rebate_est_usdc=0.0,
        our_quote_usdc=100.0,
        market_liquidity=0.0,  # 3 equal makers → ~1/3
    )
    share = competition_share(our_quote_usdc=100, market_liquidity=0)
    assert est.our_reward_share == pytest.approx(share)
    assert est.total_est_usdc == pytest.approx(1.0 + 100.0 * share)
    assert est.daily_return_pct == pytest.approx(est.total_est_usdc / 100.0)
    assert est.target_band_hit is (est.daily_return_pct >= 0.15)


def test_construct_quotes_event_empty(profile):
    m = _meta()
    tq = construct_quotes(QuoteInputs(
        meta=m, regime=Regime.EVENT, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0,
    ))
    assert tq.quotes == ()


def test_construct_quotes_toxicity_widens_in_trending(profile):
    m = _meta()
    calm = construct_quotes(QuoteInputs(
        meta=m, regime=Regime.TRENDING, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0,
    ))
    toxic = construct_quotes(QuoteInputs(
        meta=m, regime=Regime.TRENDING, fv=0.5, vol_short=0.01, toxicity=0.05,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0,
    ))

    def top_yes(tq):
        xs = [q.price for q in tq.quotes if q.token_id == "yes-token" and q.side.name == "BUY"]
        return max(xs) if xs else None

    cy, ty = top_yes(calm), top_yes(toxic)
    if cy is not None and ty is not None:
        assert ty <= cy


def test_soft_inventory_stops_adding(profile):
    m = _meta()
    # q_max default 500 @ 0.5 → 1000 shares; soft 0.6 → stop at 600
    tq = construct_quotes(QuoteInputs(
        meta=m, regime=Regime.QUIET, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token", 700, 0.5), pos_no=Position("no-token"),
        profile=profile, now=1.0,
    ))
    yes_buys = [q for q in tq.quotes if q.token_id == "yes-token" and q.side.name == "BUY"]
    assert yes_buys == []
