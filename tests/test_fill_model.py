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
    X = np.random.randn(n, 25).astype(np.float32)
    y_fill = (np.random.rand(n) > 0.4).astype(np.float32)  # ~60 fills ≥ min_samples
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
    assert 0.5 <= pred.suggested_size_mult <= 2.5
    assert 0.0 <= pred.confidence <= 1.0


def test_fill_model_needs_enough_fills_to_train_quality_models():
    """With too few fills, the markout regressor stays None → not trained."""
    import numpy as np
    model = FillModel(min_samples=50)
    np.random.seed(42)
    X = np.random.randn(100, 25).astype(np.float32)
    y_fill = (np.random.rand(100) > 0.9).astype(np.float32)  # ~10 fills < min_samples
    y_markout = np.random.randn(100).astype(np.float32) * 0.01
    model.train(X, y_fill, y_markout)
    assert model._fill_clf is not None
    assert model._markout_reg is None
    assert model._good_fill_clf is None
    assert not model.is_trained
    # Falls back to heuristic
    f = FillFeatures(
        book_imbalance=0.0, spread_ticks=3, at_touch=0.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.02,
        mid_price=0.5, our_size_vs_depth=0.1,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=4.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = model.predict(f)
    assert pred.prob_fill == 0.1
    assert pred.suggested_size_mult == 1.0


def test_sizing_signed_on_p_good():
    """size_mult must be >1 when confident-good, <1 when confident-bad."""
    import numpy as np
    model = FillModel(min_samples=20)
    np.random.seed(7)
    n = 60
    X = np.random.randn(n, 25).astype(np.float32)
    # Feature 0 drives goodness: high feature0 + low feature1 → good fill
    X[:, 0] = np.where(np.random.rand(n) > 0.5, 2.0, -2.0)
    y_fill = np.ones(n, dtype=np.float32)
    y_markout = (X[:, 0] * 0.02).astype(np.float32)
    model.train(X, y_fill, y_markout)
    assert model.is_trained

    def _feats(f0: float) -> FillFeatures:
        return FillFeatures(
            book_imbalance=f0, spread_ticks=2, at_touch=1.0,
            vol_ratio=0.1, flow_z=0.0, toxicity=0.01,
            mid_price=0.5, our_size_vs_depth=0.05,
            hours_to_resolve=168.0, quote_dist_from_mid_ticks=1.0,
            regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
            regime_reduce_only=0.0, regime_halted=0.0,
        )

    p_good = model.predict(_feats(3.0))
    p_bad = model.predict(_feats(-3.0))
    assert p_good.suggested_size_mult > 1.0
    assert p_bad.suggested_size_mult < 1.0
    assert p_bad.suggested_size_mult < p_good.suggested_size_mult


def test_quality_filter_score_tree():
    from polymaker.strategy.fill_model import quality_filter_score

    # At-touch, shallow ask, strongly ask-heavy → 1.0 (best case, WR 80%)
    assert quality_filter_score(
        imbalance=-0.4, spread_ticks=2, mid=0.5,
        bd_total=10000, ad1=1000, ad2=1000, dist_ticks=0.5,
    ) == 1.0
    # At-touch, bid-heavy into a deep ask wall → 0.0 (WR 19-24%, skip)
    assert quality_filter_score(
        imbalance=0.4, spread_ticks=2, mid=0.5,
        bd_total=10000, ad1=80000, ad2=80000, dist_ticks=0.5,
    ) == 0.0
    # Away from touch, ask-heavy → 1.0 (WR 85%)
    assert quality_filter_score(
        imbalance=-0.4, spread_ticks=4, mid=0.5,
        bd_total=10000, ad1=1000, ad2=1000, dist_ticks=3.0,
    ) == 1.0


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
    assert X.shape == (60, 25)
    assert len(yf) == 60
    assert len(ym) == 60


def test_fill_model_bundle_roundtrip(tmp_path):
    import numpy as np

    model = FillModel(min_samples=20)
    np.random.seed(123)
    X = np.random.randn(80, 25).astype(np.float32)
    y_fill = np.array([0.0, 1.0] * 40, dtype=np.float32)
    y_markout = np.array([0.002, -0.001] * 40, dtype=np.float32)
    model.train(X, y_fill, y_markout)
    store = FillTrainingStore.from_arrays(X, y_fill, y_markout)

    path = tmp_path / "fill_model.pkl"
    model.save(path, store)
    restored, restored_store = FillModel.load_bundle(path)

    assert restored.is_trained
    assert restored_store is not None
    assert len(restored_store.features) == 80
    pred = restored.predict(
        FillFeatures(
            0.1, 2.0, 1.0, 1.0, 0.0, 0.0, 0.5, 0.1,
            168.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0,
        )
    )
    assert 0.5 <= pred.suggested_size_mult <= 2.5


def test_fill_model_validate_gates_deployment():
    """Deployability is gated on holdout metrics, not just model existence."""
    import numpy as np

    rng = np.random.RandomState(3)
    n = 400
    f0 = rng.randn(n)
    X = rng.randn(n, 25).astype(np.float32)
    X[:, 0] = f0.astype(np.float32)  # feature 0 drives both labels
    y_fill = (f0 > 0).astype(np.float32)
    y_markout = (f0 * 0.03 + rng.randn(n) * 0.01).astype(np.float32)

    model = FillModel(min_samples=50)
    model.train(X, y_fill, y_markout)
    assert model.is_trained
    assert model._oos_passed is None
    assert not model.is_deployable  # never validated -> not deployable

    m1 = model.validate(X, y_fill, y_markout, min_auc=0.55, min_corr=0.05)
    assert m1["passed"]
    assert model.is_deployable
    assert m1["auc"] > 0.7 and m1["corr"] > 0.2

    # Noise data (no signal inside X): validation must fail -> stays shadow.
    Xn = rng.randn(n, 25).astype(np.float32)
    yn = rng.permutation(y_fill).astype(np.float32)
    model2 = FillModel(min_samples=50)
    model2.train(Xn, yn, y_markout)
    m2 = model2.validate(Xn, yn, y_markout, min_auc=0.60, min_corr=0.05)
    assert not m2["passed"]
    assert not model2.is_deployable


def test_fill_training_store_online_source():
    """Online/offline provenance must survive add + persistence roundtrip."""
    def feat():
        return FillFeatures(
            0.0, 2.0, 1.0, 1.0, 0.0, 0.0, 0.5, 0.1,
            168.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0,
        )

    store = FillTrainingStore()
    store.add(feat(), filled=True, markout=0.01)                     # online (default)
    store.add(feat(), filled=False, markout=0.0, source="offline")
    store.add(feat(), filled=True, markout=-0.02)                    # online

    on = store.online_arrays()
    assert on is not None and len(on[0]) == 2
    assert len(on[0][0]) == 25
    mask = store.online_mask()
    assert list(mask) == [1.0, 0.0, 1.0]

    s2 = FillTrainingStore.from_arrays(*store.raw_arrays(), online_mask=mask)
    assert list(s2.source) == ["online", "offline", "online"]

    s3 = FillTrainingStore.from_arrays(*store.raw_arrays())
    assert all(x == "offline" for x in s3.source)
    assert s3.online_arrays() is None


def test_fill_training_store_eviction_preserves_offline():
    """Over-capacity eviction drops oldest ONLINE rows, never offline."""
    def feat():
        return FillFeatures(
            0.0, 2.0, 1.0, 1.0, 0.0, 0.0, 0.5, 0.1,
            168.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0,
        )

    store = FillTrainingStore(max_samples=10)
    # 6 offline rows (the training backbone)
    for i in range(6):
        store.add(feat(), filled=bool(i % 2), markout=0.01, source="offline")
    # 8 online rows -> total 14 > cap 10
    for i in range(8):
        store.add(feat(), filled=True, markout=0.01)  # online default
    assert len(store.features) == 10
    # All 6 offline rows survive; oldest 4 online rows were evicted.
    assert store.source.count("offline") == 6
    assert store.source.count("online") == 4
    assert store.source[0] == "offline"
    # fill rate must not collapse from eviction
    assert sum(store.y_fill) >= 6


def test_offline_trainer_reconstructs_fills_from_tape(tmp_path):
    """The trainer labels touch-crossing fills, 30s markouts, tape features."""
    import json
    import math

    from scripts.train_fill_model import build_training_store

    asset = "tok-a"
    lines = []
    ts = 1_700_000_000.0
    # A drifting, varying book: mid 0.50 → 0.54 over the run with aperiodic
    # increments; trades of varying size cross the touch on odd intervals.
    mid = 0.50
    for i in range(120):
        mid += 0.0002 + 0.0001 * math.sin(i)  # aperiodic jitter
        bb, ba = mid - 0.001, mid + 0.001
        lines.append(json.dumps({
            "ts": ts, "kind": "book",
            "data": {
                "asset_id": asset,
                "bids": [{"price": bb, "size": "5000"}, {"price": bb - 0.001, "size": "5000"}],
                "asks": [{"price": ba, "size": "5000"}, {"price": ba + 0.001, "size": "5000"}],
            },
        }))
        # every other interval: a SELL print that crosses the best bid
        if i % 2 == 1:
            lines.append(json.dumps({
                "ts": ts + 15.0, "kind": "last_trade_price",
                "data": {"asset_id": asset, "price": bb,
                         "size": str(50 + 10 * (i % 3)), "side": "SELL"},
            }))
        ts += 30.0
    journal = tmp_path / "tape.jsonl"
    journal.write_text("\n".join(lines) + "\n")

    store, stats = build_training_store([journal], tick=0.001, base_size_usdc=4.0)
    assert stats["filled_candidates"] > 0, stats
    assert stats["samples"] > 0, stats
    X, yf, ym = store.raw_arrays()
    assert float(yf.mean()) > 0.0  # fills present
    # markout labels: rising mid → BUY fills positive, SELL fills negative
    assert float(ym.mean()) != 0.0
    # tape features carry variance (not constant 0/1)
    assert X[:, 3].var() > 0.0    # vol_ratio
    assert X[:, 4].var() > 0.0    # flow_z
    assert X[:, 8].var() > 0.0    # hours_to_resolve (winds down)
    # trainer samples are marked OFFLINE (never treated as live data)
    assert all(s == "offline" for s in store.source)


# ── win-rate governor (closed-loop WR control) ─────────────────────────────


def test_win_governor_learning_until_min_samples():
    """Below min_samples the governor is neutral (no blocking/scaling)."""
    from polymaker.strategy.fill_model import WinRateGovernor

    gov = WinRateGovernor(target_wr=0.70, min_samples=20)
    for _ in range(19):
        gov.record_outcome(True)
    p = gov.policy()
    assert p.mode == "learning"
    assert p.entry_size_scale == 1.0
    assert p.consensus_floor == 0.0
    assert not p.block_entries


def test_win_governor_tightens_when_wr_low():
    """Realized WR well below target -> size cut + consensus floor rises."""
    from polymaker.strategy.fill_model import WinRateGovernor

    gov = WinRateGovernor(target_wr=0.70, window=60, min_samples=20,
                          hard_floor=0.30, hard_samples=15)
    for i in range(40):
        gov.record_outcome(i % 10 < 4)  # 40% WR — below target, above floor
    p = gov.policy()
    assert p.mode == "tight"
    assert p.entry_size_scale < 1.0
    assert p.consensus_floor > 0.0
    assert not p.block_entries  # bad but not catastrophic


def test_win_governor_blocks_entries_on_collapse():
    """WR under the hard floor -> reduce-only (entries blocked)."""
    from polymaker.strategy.fill_model import WinRateGovernor

    gov = WinRateGovernor(target_wr=0.70, window=60, min_samples=20,
                          hard_floor=0.50, hard_samples=15)
    for _ in range(20):
        gov.record_outcome(False)
    p = gov.policy()
    assert p.mode == "blocked"
    assert p.block_entries


def test_win_governor_relaxes_when_wr_high():
    """WR above target + hysteresis -> modest size-up, light floor."""
    from polymaker.strategy.fill_model import WinRateGovernor

    gov = WinRateGovernor(target_wr=0.70, window=60, min_samples=20)
    for _ in range(40):
        gov.record_outcome(True)  # 100% WR
    p = gov.policy()
    assert p.mode == "relaxed"
    assert p.entry_size_scale > 1.0
    assert p.consensus_floor == 0.40  # relaxed keeps a light selectivity floor


def test_win_governor_converges_to_target_band():
    """70% WR sits in the neutral band (no action)."""
    from polymaker.strategy.fill_model import WinRateGovernor

    gov = WinRateGovernor(target_wr=0.70, window=60, min_samples=20)
    for i in range(40):
        gov.record_outcome(i % 10 < 7)  # exactly 70%
    p = gov.policy()
    assert p.mode == "neutral"
    assert abs(p.entry_size_scale - 1.0) < 0.01


def test_win_governor_rolling_window_drops_old_outcomes():
    from polymaker.strategy.fill_model import WinRateGovernor

    gov = WinRateGovernor(window=10, min_samples=5)
    for _ in range(20):
        gov.record_outcome(True)
    for _ in range(10):
        gov.record_outcome(False)
    assert gov.n_evaluated == 10
    assert gov.realized_wr == 0.0


# ── dimension migration on load ────────────────────────────────────────────


def test_fill_model_load_migrates_stale_feature_dim(tmp_path):
    """A 19-dim artifact must be repaired to the current dim at load time."""
    import numpy as np
    from polymaker.strategy.fill_model import FillModel, FillTrainingStore

    model = FillModel(min_samples=20)
    np.random.seed(5)
    n = 80
    X = np.random.randn(n, 19).astype(np.float32)
    X[:, 0] = np.where(np.random.rand(n) > 0.5, 1.5, -1.5)
    y_fill = (X[:, 0] > 0).astype(np.float32)
    y_markout = (X[:, 0] * 0.02).astype(np.float32)
    model.train(X, y_fill, y_markout)
    assert model.is_trained
    # Force the stored model's dim marker to an old value (simulate stale artifact).
    object.__setattr__(model, "_feature_dim", 19)
    store = FillTrainingStore.from_arrays(X, y_fill, y_markout)
    path = tmp_path / "stale.pkl"
    model.save(path, store)

    restored, restored_store = FillModel.load_bundle(path)
    assert restored._feature_dim == 25  # migrated
    assert restored.is_trained
    assert restored.is_deployable is False  # revalidation still required
    assert restored.fallback_count == 0
    assert restored_store is not None
    # predict() must work on current-dim features without falling back.
    from polymaker.strategy.fill_model import FillFeatures
    f = FillFeatures(
        book_imbalance=0.5, spread_ticks=2, at_touch=1.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.01,
        mid_price=0.5, our_size_vs_depth=0.05,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=1.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    pred = restored.predict(f)
    assert restored.fallback_count == 0  # real ML decision, not heuristic
    assert 0.5 <= pred.suggested_size_mult <= 2.5


def test_fill_model_predict_counts_dim_mismatch_as_fallback(tmp_path):
    """predict() on a mismatched dim counts a loud fallback (engine gates on it)."""
    import numpy as np
    from polymaker.strategy.fill_model import FillModel

    model = FillModel(min_samples=20)
    np.random.seed(6)
    n = 80
    X = np.random.randn(n, 19).astype(np.float32)
    y_fill = (np.random.rand(n) > 0.3).astype(np.float32)
    y_markout = np.random.randn(n).astype(np.float32) * 0.01
    model.train(X, y_fill, y_markout)
    object.__setattr__(model, "_feature_dim", 19)
    # Pretend current code wants 25: the model sees a mismatch.
    f = FillFeatures(
        book_imbalance=0.5, spread_ticks=2, at_touch=1.0,
        vol_ratio=0.1, flow_z=0.0, toxicity=0.01,
        mid_price=0.5, our_size_vs_depth=0.05,
        hours_to_resolve=168.0, quote_dist_from_mid_ticks=1.0,
        regime_quiet=1.0, regime_trending=0.0, regime_event=0.0,
        regime_reduce_only=0.0, regime_halted=0.0,
    )
    # Re-train in-memory models on 25-dim so only the _feature_dim marker lies.
    X25 = np.random.randn(n, 25).astype(np.float32)
    X25[:, 0] = X[:, 0]
    model.train(X25, y_fill, y_markout)
    object.__setattr__(model, "_feature_dim", 19)
    pred = model.predict(f)
    assert model.fallback_count >= 1
    assert pred.suggested_size_mult == 1.0  # heuristic path
    # A repaired model (correct marker) stops falling back.
    object.__setattr__(model, "_feature_dim", 25)
    model._fallback_count = 0
    pred2 = model.predict(f)
    assert model.fallback_count == 0
    assert 0.5 <= pred2.suggested_size_mult <= 2.5
