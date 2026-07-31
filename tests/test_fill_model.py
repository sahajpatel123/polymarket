"""Tests for Pillar 1: Forward-looking fill model."""

from polymaker.strategy.fill_model import (
    FillFeatures,
    FillModel,
    FillTrainingStore,
    extract_features,
    _heuristic_predict,
)
from polymaker.domain import MarketMeta, Quote, Regime, Side, TokenMeta


def _make_meta():
    return MarketMeta(
        condition_id="0xaa",
        question="Test",
        slug="test",
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


def test_extract_features_buy_at_touch():
    meta = _make_meta()
    q = Quote("0x01", Side.BUY, 0.500, 50.0)
    f = extract_features(
        quote=q, meta=meta, mid=0.500,
        best_bid=0.500, best_ask=0.502,
        bid_depth=500.0, ask_depth=300.0,
        vol_ratio=0.5, flow_z=0.2, toxicity=0.05,
        regime=Regime.QUIET, hours_to_resolve=168.0, now=1000.0,
    )
    assert f.at_touch == 1.0
    assert -1.0 <= f.book_imbalance <= 1.0
    assert f.regime_quiet == 1.0
    assert f.regime_trending == 0.0


def test_extract_features_quote_behind_touch():
    meta = _make_meta()
    q = Quote("0x01", Side.BUY, 0.499, 50.0)
    f = extract_features(
        quote=q, meta=meta, mid=0.500,
        best_bid=0.500, best_ask=0.502,
        bid_depth=500.0, ask_depth=300.0,
        vol_ratio=0.5, flow_z=0.2, toxicity=0.05,
        regime=Regime.TRENDING, hours_to_resolve=168.0, now=1000.0,
    )
    assert f.at_touch == 0.0
    assert f.regime_trending == 1.0
    assert f.regime_quiet == 0.0


def test_heuristic_predict_safe_quiet():
    f = FillFeatures(
        book_imbalance=0.0, spread_ticks=3, at_touch=0.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.02,
        mid_price=0.5, our_size_vs_depth=0.1,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=4.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = _heuristic_predict(f)
    assert pred.should_quote  # no reason to skip


def test_heuristic_predict_toxic_flow():
    """High tox + high flow + at touch, but bid-heavy book means positive markout."""
    f = FillFeatures(
        book_imbalance=0.7, spread_ticks=3, at_touch=1.0,
        vol_ratio=2.0, flow_z=3.0, toxicity=0.3,
        mid_price=0.5, our_size_vs_depth=0.5,
        hours_to_resolve=24.0, quote_dist_from_mid_ticks=2.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = _heuristic_predict(f)
    # Bid-heavy book + at touch → positive markout persists despite tox.
    # prob = 0.8 exactly, not > 0.8, so should_quote = True.
    assert pred.prob_fill == 0.8
    assert pred.expected_markout > 0


def test_heuristic_predict_high_prob_toxic_no_imbalance():
    """High tox + flow, no imbalance safety net → skip."""
    f = FillFeatures(
        book_imbalance=-0.1, spread_ticks=2, at_touch=1.0,
        vol_ratio=3.0, flow_z=3.5, toxicity=0.4,
        mid_price=0.5, our_size_vs_depth=0.8,
        hours_to_resolve=24.0, quote_dist_from_mid_ticks=1.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = _heuristic_predict(f)
    assert not pred.should_quote


def test_fill_model_untrained_uses_heuristic():
    model = FillModel(min_samples=100)
    f = FillFeatures(
        book_imbalance=0.0, spread_ticks=3, at_touch=0.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.02,
        mid_price=0.5, our_size_vs_depth=0.1,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=4.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = model.predict(f)
    assert pred.should_quote
    assert not model.is_trained


def test_fill_model_trains_and_predicts():
    import numpy as np
    model = FillModel(min_samples=50)
    n = 100
    np.random.seed(42)
    X = np.random.randn(n, 15).astype(np.float32)
    y_fill = (np.random.rand(n) > 0.7).astype(np.float32)
    y_markout = np.random.randn(n).astype(np.float32) * 0.01
    model.train(X, y_fill, y_markout)
    assert model.is_trained
    f = FillFeatures(
        book_imbalance=0.5, spread_ticks=2, at_touch=1.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.01,
        mid_price=0.5, our_size_vs_depth=0.05,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=3.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = model.predict(f)
    assert 0.0 <= pred.prob_fill <= 1.0
    assert isinstance(pred.should_quote, bool)


def test_training_store():
    store = FillTrainingStore(max_samples=100)
    f = FillFeatures(
        book_imbalance=0.0, spread_ticks=3, at_touch=0.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.02,
        mid_price=0.5, our_size_vs_depth=0.1,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=4.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    for i in range(60):
        store.add(f, filled=bool(i % 3 == 0), markout=0.001 if i % 2 else -0.002)
    arrs = store.to_arrays()
    assert arrs is not None
    X, yf, ym = arrs
    assert X.shape == (60, 15)
    assert len(yf) == 60
    assert len(ym) == 60
