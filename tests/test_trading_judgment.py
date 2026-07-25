"""Trade-judgment tests: DecisionFramework + quote mapping + adaptive learning."""

from __future__ import annotations

from polymaker.domain import Position, Regime, Side
from polymaker.intelligence import (
    DecisionFramework,
    MarketFeatures,
    MarketRegime,
    TradingDecision,
)
from polymaker.strategy.quoting import QuoteInputs, construct_quotes
from tests.conftest import view


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


def test_toxic_fill_makes_next_decision_more_defensive() -> None:
    """Learning path: after a toxic fill, same features → more passive placement."""
    fw = DecisionFramework()
    feats = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=40, rewards_daily_rate=80.0,
        toxicity=0.0, flow_z=0.0, vol_ratio=1.2,
    )
    fw.update_features("m1", feats)
    fw.update_microstructure("m1", 0.49, 0.51, 200, 200, 10.0)
    pre = fw.decide("m1")
    # Record benign baseline quote then a toxic fill
    fw.record_quote("m1", -2)
    fw.record_fill("m1", offset_ticks=-2, edge=-0.01, markout=-0.02)
    # Same features again
    fw.update_features("m1", feats)
    fw.update_microstructure("m1", 0.49, 0.51, 200, 200, 11.0)
    post = fw.decide("m1")
    assert pre.should_quote and post.should_quote
    # More defensive: smaller size and/or lower band frac and/or wider buy offset
    more_passive_band = post.buy_band_frac <= pre.buy_band_frac
    more_passive_size = post.size_multiplier <= pre.size_multiplier
    more_passive_offset = post.buy_offset_ticks <= pre.buy_offset_ticks  # more negative
    assert more_passive_band or more_passive_size or more_passive_offset
    # Strict: at least band or offset moves defensive after toxic feedback
    assert post.buy_band_frac < pre.buy_band_frac or post.buy_offset_ticks < pre.buy_offset_ticks


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
    # No BUY entries; at least one SELL exit for held YES
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
    def top_yes(tq):
        xs = [q.price for q in tq.quotes if q.token_id == "yes-token" and q.side is Side.BUY]
        return max(xs) if xs else None
    py, ay = top_yes(passive), top_yes(aggressive)
    assert py is not None and ay is not None
    assert ay >= py  # more aggressive = higher BUY
    band = meta.rewards_max_spread / 100.0
    assert abs(ay - 0.5) <= band + meta.tick_size + 1e-9


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
