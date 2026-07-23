"""Tests for the unified advanced quoting model."""

from __future__ import annotations

import dataclasses

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, TokenMeta
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.advanced_quoting import (
    AdvancedQuoteInputs,
    compute_advanced_quotes,
)


def _make_meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xtest",
        question="Test?",
        slug="test",
        tokens=(TokenMeta("yes-tok", "Yes"), TokenMeta("no-tok", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=5.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
        liquidity_num=10000.0,
    )


def _make_profile() -> StrategyProfile:
    from polymaker.config import StrategyProfile
    return StrategyProfile(
        gamma=0.5,
        base_size_usdc=50.0,
        q_max_usdc=500.0,
    )


def test_advanced_quotes_basic() -> None:
    """Basic test that the advanced quoting model produces valid output."""
    inp = AdvancedQuoteInputs(
        meta=_make_meta(),
        fv=0.50,
        sigma=0.01,
        yes_view=BookView(0.49, 100.0, 0.51, 100.0, 0.48, 0.52, 100.0, 100.0),
        no_view=BookView(0.49, 100.0, 0.51, 100.0, 0.48, 0.52, 100.0, 100.0),
        pos_yes=Position("yes-tok"),
        pos_no=Position("no-tok"),
        profile=_make_profile(),
        bankroll_usdc=1000.0,
        now=1700000000.0,
    )
    out = compute_advanced_quotes(inp)
    # Bid should be below ask
    assert out.bid < out.ask
    # Both should be in valid range
    assert 0.0 < out.bid < 1.0
    assert 0.0 < out.ask < 1.0
    # Half-spread should be positive
    assert out.half_spread > 0


def test_long_inventory_lowers_bid() -> None:
    """Long YES inventory should lower the bid (reduce adding)."""
    base = AdvancedQuoteInputs(
        meta=_make_meta(),
        fv=0.50,
        sigma=0.01,
        yes_view=BookView(0.49, 100.0, 0.51, 100.0, 0.48, 0.52, 100.0, 100.0),
        no_view=BookView(0.49, 100.0, 0.51, 100.0, 0.48, 0.52, 100.0, 100.0),
        pos_yes=Position("yes-tok"),
        pos_no=Position("no-tok"),
        profile=_make_profile(),
        bankroll_usdc=1000.0,
        now=1700000000.0,
    )
    long = dataclasses.replace(base, pos_yes=Position("yes-tok", size=100.0))
    out_base = compute_advanced_quotes(base)
    out_long = compute_advanced_quotes(long)
    # Long inventory should lower the reservation price
    assert out_long.reservation < out_base.reservation
