"""Trade-judgment tests: DecisionFramework + quote mapping + adaptive learning.

These drive the *shipped* decision → construct_quotes path so toxic learning
cannot claim progress while resting BUY prices get more aggressive.
"""

from __future__ import annotations

import pytest

from polymaker.domain import MarketMeta, Position, Regime, Side, TokenMeta
from polymaker.intelligence import (
    DecisionFramework,
    MarketFeatures,
    MarketRegime,
    TradingDecision,
)
from polymaker.strategy.quoting import QuoteInputs, construct_quotes
from tests.conftest import view


@pytest.fixture
def wide_meta() -> MarketMeta:
    """10¢ reward band so band_frac 0.0 vs 0.25 is more than one tick apart."""
    return MarketMeta(
        condition_id="0xwide",
        question="Wide band?",
        slug="wide-band",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=10.0,  # 10 cents
        rewards_daily_rate=80.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z",
        event_id="evt-wide",
    )


def _top_yes_buy(tq) -> float | None:
    xs = [q.price for q in tq.quotes if q.token_id == "yes-token" and q.side is Side.BUY]
    return max(xs) if xs else None


def _quotes_from_decision(meta, profile, decision: TradingDecision):
    """Map a TradingDecision the same way the engine does into construct_quotes."""
    # Wide book so join/cross do not pin every frac to the same tick.
    return construct_quotes(QuoteInputs(
        meta=meta,
        regime=Regime.QUIET,
        fv=0.5,
        vol_short=0.0,
        toxicity=0.0,
        yes_view=view(0.35, 0.65),
        no_view=view(0.35, 0.65),
        pos_yes=Position("yes-token"),
        pos_no=Position("no-token"),
        profile=profile,
        now=1.0,
        intel_skip=not decision.should_quote,
        intel_size_scale=float(decision.size_multiplier),
        intel_buy_band_frac=float(decision.buy_band_frac),
        intel_spread_mult=max(1.0, float(decision.spread_multiplier)),
        intel_buy_offset_ticks=int(decision.buy_offset_ticks),
    ))


def test_dead_market_skips_quote() -> None:
    fw = DecisionFramework()
    fw.update_features(
        "m1",
        MarketFeatures(n_trades_last_hour=0, rewards_daily_rate=0.0),
    )
    d = fw.decide("m1")
    assert d.should_quote is False
    assert d.regime is MarketRegime.DEAD
    assert "dead" in d.reason


def test_toxic_more_passive_than_quiet() -> None:
    fw = DecisionFramework()
    quiet_f = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=50, rewards_daily_rate=100.0,
        toxicity=0.0, flow_z=0.0, vol_ratio=1.0,
    )
    toxic_f = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=50, rewards_daily_rate=100.0,
        toxicity=0.05, flow_z=0.0, vol_ratio=1.0,
    )
    fw.update_features("q", quiet_f)
    fw.update_microstructure("q", 0.49, 0.51, 100, 100, 1.0)
    dq = fw.decide("q")
    fw.update_features("t", toxic_f)
    fw.update_microstructure("t", 0.49, 0.51, 100, 100, 1.0)
    dt = fw.decide("t")
    assert dq.should_quote and dt.should_quote
    assert dt.size_multiplier <= dq.size_multiplier
    assert dt.buy_band_frac <= dq.buy_band_frac


def test_stale_skips_quote() -> None:
    fw = DecisionFramework()
    fw.update_features(
        "m1",
        MarketFeatures(
            n_trades_last_hour=5,
            rewards_daily_rate=50.0,
            seconds_since_last_update=120.0,
        ),
    )
    d = fw.decide("m1")
    assert d.should_quote is False
    assert d.regime is MarketRegime.STALE


def test_toxic_fill_makes_quotes_more_defensive(wide_meta, profile) -> None:
    """Learning path: after a toxic fill, real BUY prices must not rise.

    Asserts both Decision fields *and* construct_quotes prices (the bug
    class where band_frac=0 was a no-op and resting bids got more aggressive).
    Uses a wide reward band so 0.25 vs 0.0 is not tick-rounded to the same price.
    """
    fw = DecisionFramework()
    feats = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=40, rewards_daily_rate=80.0,
        toxicity=0.0, flow_z=0.0, vol_ratio=1.2,
    )
    fw.update_features("m1", feats)
    fw.update_microstructure("m1", 0.49, 0.51, 200, 200, 10.0)
    pre = fw.decide("m1")
    pre_tq = _quotes_from_decision(wide_meta, profile, pre)
    pre_buy = _top_yes_buy(pre_tq)
    assert pre.should_quote and pre_buy is not None

    # Record baseline quote then a toxic fill (adverse markout, no edge)
    fw.record_quote("m1", pre.buy_offset_ticks)
    fw.record_fill("m1", offset_ticks=pre.buy_offset_ticks, edge=-0.01, markout=-0.02)

    fw.update_features("m1", feats)
    fw.update_microstructure("m1", 0.49, 0.51, 200, 200, 11.0)
    post = fw.decide("m1")
    post_tq = _quotes_from_decision(wide_meta, profile, post)
    post_buy = _top_yes_buy(post_tq)
    assert post.should_quote and post_buy is not None

    # Decision-level defensiveness
    assert post.buy_band_frac < pre.buy_band_frac or post.buy_offset_ticks < pre.buy_offset_ticks

    # SHIPPED path: resting BUY must be more passive (lower or equal price)
    assert post_buy <= pre_buy + 1e-12, (
        f"toxic fill made BUY more aggressive: pre={pre_buy} post={post_buy} "
        f"band_frac {pre.buy_band_frac}->{post.buy_band_frac} "
        f"offset {pre.buy_offset_ticks}->{post.buy_offset_ticks}"
    )
    # With wide band, frac drop 0.25→0.0 must strictly lower the bid
    assert post_buy < pre_buy - 1e-12, (
        f"expected strict passive move with wide band: pre={pre_buy} post={post_buy}"
    )


def test_band_frac_zero_is_more_passive_than_quarter(wide_meta, profile) -> None:
    """frac=0.0 must rest at band floor — not fall through to economic target."""
    floor_q = construct_quotes(QuoteInputs(
        meta=wide_meta, regime=Regime.QUIET, fv=0.5, vol_short=0.0, toxicity=0.0,
        yes_view=view(0.35, 0.65), no_view=view(0.35, 0.65),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_buy_band_frac=0.0,
    ))
    mid_q = construct_quotes(QuoteInputs(
        meta=wide_meta, regime=Regime.QUIET, fv=0.5, vol_short=0.0, toxicity=0.0,
        yes_view=view(0.35, 0.65), no_view=view(0.35, 0.65),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_buy_band_frac=0.25,
    ))
    py, my = _top_yes_buy(floor_q), _top_yes_buy(mid_q)
    assert py is not None and my is not None
    assert py < my - 1e-12  # strict: 0.0 more passive than 0.25
    band = wide_meta.rewards_max_spread / 100.0
    assert abs(py - (0.5 - band)) <= wide_meta.tick_size + 1e-9


def test_intel_skip_empties_entries_but_allows_exits(meta, profile) -> None:
    """Skip new risk; still unwind inventory (safety)."""
    flat = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_skip=True,
    ))
    assert flat.quotes == ()
    held = Position("yes-token", size=50.0, avg_price=0.48)
    with_inv = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_skip=True,
    ))
    buys = [q for q in with_inv.quotes if q.side is Side.BUY]
    sells = [q for q in with_inv.quotes if q.side is Side.SELL]
    assert buys == []
    assert any(q.token_id == "yes-token" for q in sells)


def test_intel_band_frac_raises_buy_within_band(meta, profile) -> None:
    """Higher band_frac should bid closer to mid while staying in reward band."""
    passive = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.0, toxicity=0.0,
        yes_view=view(0.45, 0.55), no_view=view(0.45, 0.55),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_buy_band_frac=0.0,
    ))
    aggressive = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.0, toxicity=0.0,
        yes_view=view(0.45, 0.55), no_view=view(0.45, 0.55),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_buy_band_frac=0.8,
    ))
    py, ay = _top_yes_buy(passive), _top_yes_buy(aggressive)
    assert py is not None and ay is not None
    assert ay >= py  # more aggressive = higher BUY
    band = meta.rewards_max_spread / 100.0
    assert abs(ay - 0.5) <= band + meta.tick_size + 1e-9


def test_adaptive_offset_after_toxic_is_used() -> None:
    """Toxic learn steps market_offsets; get_band_position must honor it."""
    fw = DecisionFramework()
    feats = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=40, rewards_daily_rate=80.0,
        toxicity=0.0, flow_z=0.0, vol_ratio=1.0,
    )
    fw.update_features("m1", feats)
    fw.update_microstructure("m1", 0.49, 0.51, 200, 200, 10.0)
    pre = fw.decide("m1")
    fw.record_quote("m1", pre.buy_offset_ticks)
    fw.record_fill("m1", offset_ticks=pre.buy_offset_ticks, edge=-0.01, markout=-0.02)
    post = fw.decide("m1")
    # buy_offset more negative (or equal only if already at max) after toxic
    assert post.buy_offset_ticks <= pre.buy_offset_ticks
    # And adaptive state actually stored the stepped offset
    state = fw.get_state("m1")
    assert "m1" in state.adaptive.market_offsets
    buy, sell = state.adaptive.get_band_position("m1")
    assert buy == -abs(state.adaptive.market_offsets["m1"])
    assert buy <= -state.adaptive.base_delta_min_ticks


def test_halted_regime_still_empties_with_intel_fields(meta, profile) -> None:
    tq = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.HALTED, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0,
        intel_size_scale=2.0, intel_buy_band_frac=1.0, intel_skip=False,
    ))
    assert tq.quotes == ()


def test_event_regime_still_empties(meta, profile) -> None:
    tq = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.EVENT, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"), pos_no=Position("no-token"),
        profile=profile, now=1.0, intel_buy_band_frac=1.0,
    ))
    assert tq.quotes == ()


def test_trading_decision_contract_fields() -> None:
    d = TradingDecision(
        market_id="x", should_quote=True, regime=MarketRegime.QUIET,
        buy_band_frac=0.3, size_multiplier=0.5, reason="unit",
    )
    assert d.should_quote is True
    assert 0.0 <= d.buy_band_frac <= 1.0
    assert d.size_multiplier == 0.5
