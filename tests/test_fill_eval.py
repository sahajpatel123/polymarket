"""Tests for the honest fill-model evaluation harness.

The point of these tests is that the harness must be able to say NO. A
measuring stick that only ever reports improvement is worthless, so several
tests assert that a skill-free model is correctly rejected.
"""

from __future__ import annotations

import numpy as np
import pytest

from polymaker.strategy.fill_eval import (
    bootstrap_ci,
    brier_score,
    cluster_bootstrap_ci,
    compare_to_control,
    evaluate_gate,
    expected_calibration_error,
    grouped_folds,
    purged_time_folds,
    random_gate_control,
    roc_auc,
)

# ── splitting: the leakage guarantees ────────────────────────────────────


def test_grouped_folds_never_leak_an_asset_across_train_and_test() -> None:
    groups = np.array(["a"] * 30 + ["b"] * 20 + ["c"] * 15 + ["d"] * 10 + ["e"] * 5)
    folds = grouped_folds(groups, n_folds=5)
    assert folds, "expected at least one usable fold"
    for tr, te in folds:
        assert set(groups[tr]).isdisjoint(set(groups[te])), (
            "asset appeared in both train and test — this is the exact leak "
            "that inflates offline win rate"
        )
        assert tr.size > 0 and te.size > 0


def test_grouped_folds_cover_every_sample_exactly_once_as_test() -> None:
    groups = np.array(["a"] * 10 + ["b"] * 10 + ["c"] * 10 + ["d"] * 10)
    folds = grouped_folds(groups, n_folds=4)
    seen = np.concatenate([te for _tr, te in folds])
    assert np.array_equal(np.sort(seen), np.arange(groups.size))


def test_grouped_folds_rejects_single_asset() -> None:
    with pytest.raises(ValueError, match="needs >=2 distinct groups"):
        grouped_folds(np.array(["only"] * 50))


def test_grouped_folds_is_deterministic() -> None:
    groups = np.array(["a"] * 7 + ["b"] * 7 + ["c"] * 7)
    a = grouped_folds(groups, n_folds=3)
    b = grouped_folds(groups, n_folds=3)
    assert [x.tolist() for _t, x in a] == [x.tolist() for _t, x in b]


def test_purged_time_folds_train_is_always_before_test_with_embargo() -> None:
    ts = np.arange(0.0, 1000.0, 1.0)
    folds = purged_time_folds(ts, n_folds=4, embargo_s=30.0)
    assert folds
    for tr, te in folds:
        assert ts[tr].max() <= ts[te].min() - 30.0, (
            "train label horizon overlaps the test window — 30s forward "
            "markout labels would leak"
        )


def test_purged_time_folds_handles_tiny_input() -> None:
    assert purged_time_folds(np.array([1.0, 2.0]), n_folds=4) == []


# ── uncertainty: the interval must widen when evidence is weaker ─────────


def test_cluster_bootstrap_is_wider_than_row_bootstrap_when_rows_correlate() -> None:
    """Within-asset correlation means row-level CIs are dishonestly narrow."""
    rng = np.random.RandomState(0)
    vals, clus = [], []
    for a in range(4):  # only 4 assets, 200 rows each
        level = rng.choice([0.0, 1.0])  # asset-level outcome; rows are copies
        vals.append(np.full(200, level))
        clus.append(np.full(200, f"asset{a}"))
    v = np.concatenate(vals)
    c = np.concatenate(clus)
    _m1, lo1, hi1 = bootstrap_ci(v, n_boot=500, seed=1)
    _m2, lo2, hi2, n_cl = cluster_bootstrap_ci(v, c, n_boot=500, seed=1)
    assert n_cl == 4
    assert (hi2 - lo2) > (hi1 - lo1), (
        "cluster bootstrap must be wider; otherwise 4 assets masquerade as "
        "800 independent samples"
    )


def test_cluster_bootstrap_reports_cluster_count_not_row_count() -> None:
    v = np.array([1.0, 0.0, 1.0, 1.0, 0.0, 1.0])
    c = np.array(["x", "x", "x", "y", "y", "y"])
    _m, _lo, _hi, n_cl = cluster_bootstrap_ci(v, c, n_boot=200)
    assert n_cl == 2


def test_cluster_bootstrap_single_cluster_returns_nan_interval() -> None:
    v = np.array([1.0, 0.0, 1.0])
    c = np.array(["x", "x", "x"])
    mean, lo, hi, n_cl = cluster_bootstrap_ci(v, c, n_boot=100)
    assert n_cl == 1
    assert mean == pytest.approx(2 / 3)
    assert np.isnan(lo) and np.isnan(hi), "one asset cannot yield an interval"


# ── metrics ──────────────────────────────────────────────────────────────


def test_roc_auc_perfect_and_inverted_and_random() -> None:
    y = np.array([0, 0, 1, 1], dtype=float)
    assert roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)
    assert roc_auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)


def test_roc_auc_matches_sklearn() -> None:
    sk = pytest.importorskip("sklearn.metrics")
    rng = np.random.RandomState(3)
    y = rng.randint(0, 2, size=300).astype(float)
    s = rng.rand(300) * 0.4 + y * 0.3
    assert roc_auc(y, s) == pytest.approx(sk.roc_auc_score(y, s), abs=1e-9)


def test_roc_auc_single_class_is_nan() -> None:
    assert np.isnan(roc_auc(np.ones(5), np.linspace(0, 1, 5)))


def test_brier_and_ece_reward_calibration_not_just_ranking() -> None:
    """Identical ranking, different calibration.

    AUC cannot distinguish these two, but the governor maps a consensus score
    onto an expected win rate, so calibration is what actually matters to it.
    """
    y = np.array([0.0, 0.0, 0.0, 1.0])            # base rate 0.25
    calibrated = np.array([0.10, 0.15, 0.20, 0.90])
    overconfident = np.array([0.70, 0.75, 0.80, 0.95])
    # identical perfect ranking ...
    assert roc_auc(y, calibrated) == pytest.approx(1.0)
    assert roc_auc(y, overconfident) == pytest.approx(1.0)
    # ... but the overconfident model is badly calibrated
    assert brier_score(y, calibrated) < brier_score(y, overconfident)
    assert expected_calibration_error(y, calibrated, n_bins=5) < (
        expected_calibration_error(y, overconfident, n_bins=5)
    )


def test_ece_bin_edges_are_exact_not_float_fuzzy() -> None:
    """A probability exactly on a bin edge must land in the upper bin."""
    y = np.array([0.0, 1.0])
    # 0.6 sits exactly on an edge for n_bins=5; float edges used to merge bins
    # and report a perfectly calibrated 0.0 for a miscalibrated model.
    assert expected_calibration_error(y, np.array([0.6, 0.6]), n_bins=5) == (
        pytest.approx(0.1)
    )


def test_ece_is_zero_for_perfect_predictions() -> None:
    y = np.array([0.0, 0.0, 1.0, 1.0])
    assert expected_calibration_error(y, y, n_bins=10) == pytest.approx(0.0)


# ── the control: a skill-free gate must be rejected ──────────────────────


def test_random_gate_control_matches_requested_retention() -> None:
    rng = np.random.RandomState(5)
    mk = rng.randn(500)
    wr, lo, hi = random_gate_control(mk, np.repeat("a", 500), 0.5, n_trials=200)
    base = float((mk > 0).mean())
    assert lo <= base <= hi, "random gate must straddle the unconditional rate"
    assert wr == pytest.approx(base, abs=0.05)


def test_skill_free_gate_does_not_beat_control() -> None:
    """A gate that selects at random must be reported as NOT better."""
    rng = np.random.RandomState(7)
    n, n_assets = 800, 8
    mk = rng.randn(n) * 0.05
    clusters = np.array([f"a{i % n_assets}" for i in range(n)])
    taken = rng.rand(n) < 0.5           # no information used
    res = evaluate_gate(mk, taken, clusters, n_boot=400)
    ctrl_wr, _lo, ctrl_hi = random_gate_control(
        mk, clusters, res.retention, n_trials=200
    )
    verdict = compare_to_control(res, ctrl_wr, ctrl_hi)
    assert verdict["beats_control"] is False, (
        "harness accepted a gate with zero information — it cannot be trusted "
        "to validate real models"
    )


def test_genuinely_skillful_gate_beats_control() -> None:
    """A gate with real information must be detected as better."""
    rng = np.random.RandomState(11)
    n, n_assets = 2000, 12
    mk = rng.randn(n) * 0.05
    clusters = np.array([f"a{i % n_assets}" for i in range(n)])
    taken = mk > 0                       # oracle gate
    res = evaluate_gate(mk, taken, clusters, n_boot=400)
    ctrl_wr, _lo, ctrl_hi = random_gate_control(
        mk, clusters, res.retention, n_trials=200
    )
    verdict = compare_to_control(res, ctrl_wr, ctrl_hi)
    assert res.win_rate == pytest.approx(1.0)
    assert verdict["beats_control"] is True
    assert verdict["lift_vs_control"] is not None and verdict["lift_vs_control"] > 0.3


def test_four_asset_evidence_is_too_weak_to_certify() -> None:
    """The shipped model's test set shape: 4 assets, correlated rows.

    Even a gate that looks good on the point estimate must fail the
    non-overlapping-CI test when there are only 4 clusters of evidence.
    """
    rng = np.random.RandomState(13)
    vals, clus = [], []
    for a in range(4):
        # asset-level bias: 3 good assets, 1 bad -> point estimate looks great
        bias = 0.02 if a < 3 else -0.06
        vals.append(rng.randn(180) * 0.03 + bias)
        clus.append(np.full(180, f"asset{a}"))
    mk = np.concatenate(vals)
    clusters = np.concatenate(clus)
    taken = rng.rand(mk.size) < 0.52
    res = evaluate_gate(mk, taken, clusters, n_boot=500)
    assert res.n_clusters == 4
    ctrl_wr, _lo, ctrl_hi = random_gate_control(
        mk, clusters, res.retention, n_trials=200
    )
    verdict = compare_to_control(res, ctrl_wr, ctrl_hi)
    assert verdict["beats_control"] is False
    assert res.wr_hi - res.wr_lo > 0.05, (
        "4-cluster interval should be visibly wide; a narrow one would mean "
        "the bootstrap is not resampling assets"
    )


# ── gate accounting ──────────────────────────────────────────────────────


def test_evaluate_gate_accounting_is_exact() -> None:
    mk = np.array([0.1, -0.2, 0.3, -0.4, 0.5, 0.6])
    taken = np.array([True, True, False, False, True, True])
    clusters = np.array(["a", "a", "b", "b", "b", "c"])
    res = evaluate_gate(mk, taken, clusters, n_boot=200)
    assert res.n_total == 6
    assert res.n_taken == 4
    assert res.retention == pytest.approx(4 / 6)
    assert res.total_markout == pytest.approx(0.1 - 0.2 + 0.5 + 0.6)
    assert res.win_rate == pytest.approx(0.75)  # 3 of 4 taken are > 0


def test_evaluate_gate_with_nothing_taken_is_reported_not_crashed() -> None:
    mk = np.array([0.1, -0.2, 0.3])
    res = evaluate_gate(mk, np.zeros(3, dtype=bool), np.array(["a", "a", "b"]))
    assert res.n_taken == 0
    assert res.retention == 0.0
    assert np.isnan(res.win_rate)


def test_compare_to_control_requires_non_overlapping_intervals() -> None:
    mk = np.array([0.1, 0.2, -0.1, 0.3, 0.4, -0.2] * 20)
    clusters = np.array([f"a{i%6}" for i in range(len(mk))])
    res = evaluate_gate(mk, mk > 0, clusters, n_boot=300)
    # control upper bound deliberately placed above the model lower bound
    overlapping = compare_to_control(res, 0.5, res.wr_lo + 0.01)
    assert overlapping["beats_control"] is False
    clearly_better = compare_to_control(res, 0.1, res.wr_lo - 0.01)
    assert clearly_better["beats_control"] is True
