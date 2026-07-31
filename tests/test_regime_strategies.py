"""Tests for Pillar 2: Regime-conditional strategies."""

import math

from polymaker.strategy.regime_strategies import (
    StrategyContext,
    _benign_strategy,
    _convexity_strategy,
    _detect_queue_war,
    _exit_only_strategy,
    _toxic_strategy,
    dispatch_strategy,
)
from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Regime, Side, TokenMeta
from polymaker.marketdata.orderbook import BookView


def _meta():
    return MarketMeta(
        condition_id="0xbb",
        question="Test Market",
        slug="test-market",
        tokens=(TokenMeta("0x01", "Yes"), TokenMeta("0x02", "No")),
        tick_size=0.001,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=100.0,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
    )


def _profile():
    return StrategyProfile(
        base_size_usdc=20.0,
        q_max_usdc=100.0,
        delta_min_ticks=2,
        layer_step_ticks=2,
        gamma=0.1,
        c_vol=1.0,
        c_tox=10.0,
        layers=3,
        min_edge_ticks=2,
        q_soft_frac=0.8,
        reward_size_mult=1.0,
    )


def _view(bid=None, ask=None, bid_sz=100.0, ask_sz=100.0):
    return BookView(
        best_bid=bid, best_bid_size=bid_sz,
        best_ask=ask, best_ask_size=ask_sz,
        second_bid=None, second_ask=None,
        bid_depth=bid_sz, ask_depth=ask_sz,
    )


def _ctx(regime=Regime.QUIET, fv=0.5, **kw):
    defaults = dict(
        meta=_meta(),
        regime=regime,
        fv=fv,
        vol_short=0.005,
        toxicity=0.02,
        yes_view=_view(0.498, 0.502),
        no_view=_view(0.496, 0.500),
        pos_yes=Position("0x01", 0.0, 0.0),
        pos_no=Position("0x02", 0.0, 0.0),
        profile=_profile(),
        now=1000.0,
    )
    defaults.update(kw)
    return StrategyContext(**defaults)


def test_dispatch_halted_empty():
    tq = dispatch_strategy(_ctx(regime=Regime.HALTED))
    assert len(tq.quotes) == 0
    assert tq.regime == Regime.HALTED


def test_dispatch_event_empty():
    tq = dispatch_strategy(_ctx(regime=Regime.EVENT))
    assert len(tq.quotes) == 0


def test_dispatch_quiet_produces_quotes():
    tq = dispatch_strategy(_ctx(regime=Regime.QUIET))
    assert len(tq.quotes) > 0


def test_toxic_strategy_aggressive_exits():
    pos_yes = Position("0x01", 20.0, 0.5)
    tq = _toxic_strategy(_ctx(regime=Regime.TRENDING, pos_yes=pos_yes, toxicity=0.2))
    # Should produce exits and possibly entries
    assert any(q.side == Side.SELL for q in tq.quotes)


def test_exit_only_strategy_no_entries():
    pos_yes = Position("0x01", 30.0, 0.5)
    pos_no = Position("0x02", 10.0, 0.5)
    tq = _exit_only_strategy(_ctx(
        regime=Regime.REDUCE_ONLY,
        pos_yes=pos_yes, pos_no=pos_no,
        yes_exit_urgency=0.8, no_exit_urgency=0.8,
    ))
    assert not any(q.side == Side.BUY for q in tq.quotes)
    assert any(q.side == Side.SELL for q in tq.quotes)


def test_convexity_near_yes_resolve():
    pos_yes = Position("0x01", 10.0, 0.5)
    pos_no = Position("0x02", 20.0, 0.5)
    tq = _convexity_strategy(_ctx(fv=0.95, pos_yes=pos_yes, pos_no=pos_no))
    # Should exit NO, trade YES
    sells = [q for q in tq.quotes if q.side == Side.SELL]
    buy_yes = [q for q in tq.quotes if q.side == Side.BUY and q.token_id == "0x01"]
    buy_no = [q for q in tq.quotes if q.side == Side.BUY and q.token_id == "0x02"]
    assert len(sells) > 0  # exits NO
    assert len(buy_yes) > 0  # buys YES
    assert len(buy_no) == 0  # no NO buys


def test_convexity_near_no_resolve():
    pos_yes = Position("0x01", 20.0, 0.5)
    pos_no = Position("0x02", 10.0, 0.5)
    tq = _convexity_strategy(_ctx(fv=0.05, pos_yes=pos_yes, pos_no=pos_no))
    sells = [q for q in tq.quotes if q.side == Side.SELL]
    buy_no = [q for q in tq.quotes if q.side == Side.BUY and q.token_id == "0x02"]
    buy_yes = [q for q in tq.quotes if q.side == Side.BUY and q.token_id == "0x01"]
    assert len(sells) > 0  # exits YES
    assert len(buy_no) > 0  # buys NO
    assert len(buy_yes) == 0  # no YES buys


def test_benign_strategy_flat_inventory():
    tq = _benign_strategy(_ctx())
    assert len(tq.quotes) > 0


def test_detect_queue_war_false_on_spread():
    ctx = _ctx()
    assert not _detect_queue_war(ctx)


def test_detect_queue_war_true():
    ctx = _ctx(
        yes_view=_view(0.499, 0.500, bid_sz=500.0, ask_sz=300.0),
        no_view=_view(0.498, 0.499, bid_sz=500.0, ask_sz=300.0),
    )
    assert _detect_queue_war(ctx)
