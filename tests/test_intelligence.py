"""Tests for the intelligence layer: adaptive spread, regime detection,
microstructure features, and decision framework.
"""

from __future__ import annotations

from polymaker.intelligence.adaptive_spread import (
    AdaptiveSpreadParams,
    MarketFillStats,
)
from polymaker.intelligence.microstructure import (
    MicrostructureTracker,
    compute_adverse_selection_risk,
    compute_depth_imbalance,
    compute_fill_probability,
    compute_microprice,
    compute_opportunity_score,
)
from polymaker.intelligence.regime_detector import (
    MarketFeatures,
    MarketRegime,
    detect_regime,
)

# ── AdaptiveSpreadParams tests ─────────────────────────────────────────


def test_adaptive_spread_default_offsets():
    """Default offsets are +/- delta_min_ticks when no fills yet."""
    p = AdaptiveSpreadParams(base_delta_min_ticks=3)
    buy_off, sell_off = p.get_band_position("0xnew")
    assert buy_off == -3
    assert sell_off == 3


def test_adaptive_spread_learns_from_fills():
    """After several fills at offset +2, the optimal offset becomes +2."""
    p = AdaptiveSpreadParams(base_delta_min_ticks=3)
    cid = "0xtest"
    # Simulate 10 quotes at offset +2, 5 of which fill
    for _ in range(10):
        p.record_quote(cid, 2)
    for _ in range(5):
        p.learn_from_fill(cid, offset_ticks=2, edge=0.01, markout=-0.002)
    # Simulate 10 quotes at offset +5, only 1 of which fills
    for _ in range(10):
        p.record_quote(cid, 5)
    p.learn_from_fill(cid, offset_ticks=5, edge=0.01, markout=-0.002)
    # Optimal offset should be +2 (higher fill rate)
    buy_off, sell_off = p.get_band_position(cid)
    assert sell_off == 2


def test_adaptive_spread_ignores_negative_edge_offsets():
    """Offsets with negative edge are not selected as optimal."""
    p = AdaptiveSpreadParams(base_delta_min_ticks=3)
    cid = "0xtest"
    # Offset 5: high fill rate but negative edge
    for _ in range(10):
        p.record_quote(cid, 5)
    for _ in range(8):
        p.learn_from_fill(cid, offset_ticks=5, edge=-0.01, markout=-0.005)
    # Offset 2: lower fill rate but positive edge
    for _ in range(10):
        p.record_quote(cid, 2)
    for _ in range(3):
        p.learn_from_fill(cid, offset_ticks=2, edge=0.01, markout=-0.002)
    # Optimal offset should be 2 (positive edge) not 5 (negative edge)
    buy_off, sell_off = p.get_band_position(cid)
    assert sell_off == 2


def test_market_fill_stats_overall_rate():
    """Overall fill rate is computed correctly."""
    s = MarketFillStats()
    for _ in range(10):
        s.record_quote(0)
    for _ in range(3):
        s.record_fill(0, 0.01, -0.001)
    assert abs(s.get_fill_rate() - 0.3) < 0.01
    assert abs(s.get_avg_edge() - 0.01) < 0.001


# ── detect_regime tests ──────────────────────────────────────────────────


def test_regime_dead_market():
    """Dead market: no trades, no rewards → skip."""
    f = MarketFeatures(
        n_trades_last_hour=0,
        rewards_daily_rate=0.0,
    )
    d = detect_regime(f)
    assert d.regime == MarketRegime.DEAD
    assert not d.should_quote
    assert d.skip_reason == "dead_market"


def test_regime_stale_data():
    """Stale data: no recent updates → skip."""
    f = MarketFeatures(
        seconds_since_last_update=120.0,
        rewards_daily_rate=100.0,
        n_trades_last_hour=10,
    )
    d = detect_regime(f)
    assert d.regime == MarketRegime.STALE
    assert not d.should_quote


def test_regime_toxic():
    """High toxicity → quote with 2x spread, half size."""
    f = MarketFeatures(
        toxicity=0.02,
        rewards_daily_rate=100.0,
        n_trades_last_hour=10,
    )
    d = detect_regime(f)
    assert d.regime == MarketRegime.TOXIC
    assert d.should_quote
    assert d.spread_multiplier == 2.0
    assert d.size_multiplier == 0.5


def test_regime_volatile():
    """High vol ratio → quote with 1.5x spread, half size."""
    f = MarketFeatures(
        vol_ratio=6.0,
        rewards_daily_rate=100.0,
        n_trades_last_hour=10,
    )
    d = detect_regime(f)
    assert d.regime == MarketRegime.VOLATILE
    assert d.should_quote
    assert d.spread_multiplier == 1.5


def test_regime_trending():
    """Strong flow → trending regime."""
    f = MarketFeatures(
        flow_z=2.5,
        rewards_daily_rate=100.0,
        n_trades_last_hour=10,
    )
    d = detect_regime(f)
    assert d.regime == MarketRegime.TRENDING
    assert d.should_quote
    assert d.spread_multiplier == 1.5


def test_regime_quiet_default():
    """Default: quiet, normal parameters."""
    f = MarketFeatures(
        rewards_daily_rate=100.0,
        n_trades_last_hour=10,
        flow_z=0.5,
        vol_ratio=1.5,
        toxicity=0.001,
    )
    d = detect_regime(f)
    assert d.regime == MarketRegime.QUIET
    assert d.should_quote
    assert d.spread_multiplier == 1.0
    assert d.size_multiplier == 1.0


# ── compute_* function tests ────────────────────────────────────────────


def test_compute_microprice_balanced():
    """Balanced book: microprice = mid."""
    mp = compute_microprice(0.49, 100, 0.51, 100)
    assert abs(mp - 0.50) < 1e-9


def test_compute_microprice_ask_heavy():
    """Heavy ask side → microprice > mid (price likely up)."""
    mp = compute_microprice(0.49, 100, 0.51, 300)
    # mp = (0.49 * 300 + 0.51 * 100) / 400 = 0.50
    assert abs(mp - 0.50) < 0.01


def test_compute_depth_imbalance_balanced():
    assert compute_depth_imbalance(100, 100) == 0.0


def test_compute_depth_imbalance_bid_heavy():
    imb = compute_depth_imbalance(200, 100)
    assert abs(imb - 1.0/3.0) < 0.01


def test_compute_fill_probability_higher_at_tight_offset():
    """Tighter offset = higher fill probability."""
    p_tight = compute_fill_probability(0.0, 0.0, 1.0, 0.0, offset_ticks=0.0)
    p_wide = compute_fill_probability(0.0, 0.0, 1.0, 0.0, offset_ticks=5.0)
    assert p_tight > p_wide


def test_compute_fill_probability_higher_with_imbalance():
    """Higher imbalance (in our direction) = higher fill probability."""
    p_calm = compute_fill_probability(0.0, 0.0, 1.0, 0.0, offset_ticks=1.0)
    p_imb = compute_fill_probability(0.5, 0.5, 1.0, 0.0, offset_ticks=1.0)
    assert p_imb > p_calm


def test_compute_adverse_selection_risk_zero_at_calm():
    """Zero imbalance and zero flow = zero AS risk."""
    risk = compute_adverse_selection_risk(0.0, 0.0, 0.0)
    assert risk == 0.0


def test_compute_adverse_selection_risk_higher_with_imbalance():
    """Higher imbalance = higher AS risk."""
    r_calm = compute_adverse_selection_risk(0.0, 0.0, 0.001)
    r_imb = compute_adverse_selection_risk(0.5, 0.5, 0.001)
    assert r_imb > r_calm


def test_compute_opportunity_score_zero_at_high_risk():
    """Opportunity score is zero when AS risk is 100%."""
    score = compute_opportunity_score(fill_prob=0.5, expected_edge=0.01, as_risk=1.0)
    assert score == 0.0


# ── MicrostructureTracker tests ────────────────────────────────────────


def test_tracker_basic_extraction():
    """Tracker extracts features from book + trade updates."""
    t = MicrostructureTracker()
    t.update_book(0.49, 0.51, 100, 100, 1000.0)
    t.update_trade("BUY", 0.51, 10, 1001.0)
    t.update_trade("SELL", 0.49, 5, 1002.0)
    f = t.extract()
    assert f.microprice > 0
    assert -1 <= f.depth_imbalance <= 1
    assert f.flow_count == 2
    assert f.flow_imbalance != 0


def test_tracker_empty_extract():
    """Empty tracker returns zero features."""
    t = MicrostructureTracker()
    f = t.extract()
    assert f.microprice == 0.0
    assert f.depth_imbalance == 0.0
    assert f.flow_count == 0


# ── DecisionFramework tests ───────────────────────────────────────────


def test_decision_framework_basic():
    """Framework returns a decision for a known market."""
    from polymaker.intelligence.decision import DecisionFramework, IntelligenceState

    fw = DecisionFramework()
    cid = "0xtest"
    state = IntelligenceState(condition_id=cid)
    fw.states[cid] = state
    # Default features: quiet, active
    state.regime_features = MarketFeatures(
        rewards_daily_rate=100.0,
        n_trades_last_hour=10,
    )
    d = fw.decide(cid)
    assert d.should_quote
    assert d.regime == MarketRegime.QUIET


def test_decision_framework_skips_dead_market():
    """Framework skips dead markets."""
    from polymaker.intelligence.decision import DecisionFramework, IntelligenceState

    fw = DecisionFramework()
    cid = "0xdead"
    state = IntelligenceState(condition_id=cid)
    state.regime_features = MarketFeatures(
        n_trades_last_hour=0, rewards_daily_rate=0.0
    )
    fw.states[cid] = state
    d = fw.decide(cid)
    assert not d.should_quote
    assert d.reason == "dead_market"


def test_decision_framework_ranks_by_opportunity():
    """Higher opportunity score = higher rank."""
    from polymaker.intelligence.decision import DecisionFramework, IntelligenceState

    fw = DecisionFramework()
    # Market A: high opportunity
    state_a = IntelligenceState(condition_id="A")
    state_a.regime_features = MarketFeatures(
        rewards_daily_rate=200.0, n_trades_last_hour=50
    )
    state_a.last_decision = type('D', (), {
        'should_quote': True, 'opportunity_score': 0.8
    })()
    fw.states["A"] = state_a
    # Market B: low opportunity
    state_b = IntelligenceState(condition_id="B")
    state_b.regime_features = MarketFeatures(
        rewards_daily_rate=50.0, n_trades_last_hour=5
    )
    state_b.last_decision = type('D', (), {
        'should_quote': True, 'opportunity_score': 0.2
    })()
    fw.states["B"] = state_b
    ranked = fw.rank_markets(["A", "B"])
    assert ranked[0] == "A"
    assert ranked[1] == "B"


def test_decision_framework_record_fill_updates_adaptive():
    """Recording a fill updates the adaptive spread parameters."""
    from polymaker.intelligence.decision import DecisionFramework, IntelligenceState

    fw = DecisionFramework()
    cid = "0xtest"
    state = IntelligenceState(condition_id=cid)
    state.regime_features = MarketFeatures(
        rewards_daily_rate=100.0, n_trades_last_hour=10
    )
    fw.states[cid] = state
    # Record fills at offset +2
    for _ in range(5):
        fw.record_quote(cid, 2)
    for _ in range(3):
        fw.record_fill(cid, offset_ticks=2, edge=0.01, markout=-0.001)
    # Now decision should use offset +2 for SELL
    d = fw.decide(cid)
    assert d.sell_offset_ticks == 2
    assert d.buy_offset_ticks == -2
