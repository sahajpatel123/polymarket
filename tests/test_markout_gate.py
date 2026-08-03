"""The markout-correlation gate must be measurable, and honest when it is not.

A silently-skipped metric that defaults to a failing value is the worst of both
worlds: it blocks deployment forever while looking like a legitimate rejection.

That is what happened. The markout regressor required >= min_samples (100) FILL
rows inside the 70/30 train split. Fills are ~1.5% of rows, so a 4-hour live run
that banked 7,943 rows carried only 88 fills into train — under the threshold, so
the regressor was skipped and ``corr`` defaulted to 0.0, which fails the 0.05
floor. The engine logged ``auc=0.998 corr=0.0 deployable=False`` on every retrain
for the whole run. The same 119 fills score +0.51 under K-fold CV.

The metric is now cross-validated over the fill rows, and ``corr_computed`` makes
"not measurable" distinguishable from "no skill". The gate still fails closed.
"""

from __future__ import annotations

import numpy as np
import pytest

from polymaker.strategy.fill_model import (
    _MIN_FILLS_FOR_MARKOUT_CV,
    FillModel,
)

N_FEATURES = 25


def _synth(n_rows: int, n_fills: int, *, signal: bool = True,
           seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sparse-fill data shaped like a real online buffer (~1.5% fills)."""
    rng = np.random.RandomState(seed)
    X = rng.randn(n_rows, N_FEATURES).astype(np.float32)
    y_fill = np.zeros(n_rows, dtype=np.float32)
    idx = rng.choice(n_rows, size=n_fills, replace=False)
    y_fill[idx] = 1.0
    # make fills separable so AUC is not the thing under test
    X[idx, 18] += 6.0
    y_mk = np.zeros(n_rows, dtype=np.float32)
    if signal:
        # markout is a learnable function of a feature
        y_mk[idx] = (X[idx, 0] * 0.01).astype(np.float32)
    else:
        y_mk[idx] = rng.randn(n_fills).astype(np.float32) * 0.01
    return X, y_fill, y_mk


def _trained(X, yf, ym) -> FillModel:
    m = FillModel(min_samples=100)
    m.train(X, yf, ym)
    assert m.is_trained
    return m


def _healthy_model() -> FillModel:
    """A trained model, so evaluation data can be varied independently.

    FillModel.train() needs >= min_samples FILL rows before the markout
    regressor is fitted, so a few-fill dataset cannot both train and be scored.
    """
    X, yf, ym = _synth(8000, 200, signal=True, seed=99)
    return _trained(X, yf, ym)


def test_correlation_is_computed_with_few_fills_in_a_large_buffer() -> None:
    """The exact shape that used to skip: many rows, ~1.5% fills."""
    X, yf, ym = _synth(7943, 119, signal=True)
    m = _trained(X, yf, ym)
    r = m.holdout_metrics(X, yf, ym, min_auc=0.55, min_corr=0.05)
    assert r["corr_computed"] is True, (
        "markout correlation was skipped again — the gate would never open"
    )
    assert r["n_fills"] == 119


def test_reported_correlation_is_not_a_silent_zero() -> None:
    X, yf, ym = _synth(4000, 80, signal=True)
    m = _healthy_model()
    r = m.holdout_metrics(X, yf, ym, min_auc=0.55, min_corr=0.05)
    assert r["corr_computed"] is True
    assert float(r["corr"]) != 0.0


def test_not_measurable_is_distinguishable_from_no_skill() -> None:
    """Too few fills must say so, not report a failing 0.0."""
    X, yf, ym = _synth(2000, _MIN_FILLS_FOR_MARKOUT_CV - 5, signal=True)
    m = _healthy_model()
    r = m.holdout_metrics(X, yf, ym, min_auc=0.55, min_corr=0.05)
    assert r["corr_computed"] is False
    assert r["passed"] is False, "must still fail closed"
    assert "not computable" in str(r["reason"])


def test_gate_fails_closed_when_correlation_is_unmeasurable() -> None:
    X, yf, ym = _synth(2000, 5, signal=True)
    m = _healthy_model()
    m.validate(X, yf, ym, min_auc=0.0, min_corr=0.0)
    assert m.is_deployable is False, (
        "deployed on evidence that could not be measured"
    )


def test_genuine_absence_of_signal_is_rejected() -> None:
    """Random markouts must not pass, even with plenty of fills."""
    X, yf, ym = _synth(6000, 150, signal=False, seed=3)
    m = _trained(X, yf, ym)
    r = m.holdout_metrics(X, yf, ym, min_auc=0.55, min_corr=0.30)
    assert r["corr_computed"] is True
    assert float(r["corr"]) < 0.30
    assert r["passed"] is False


def test_learnable_signal_passes_the_floor() -> None:
    X, yf, ym = _synth(6000, 150, signal=True, seed=4)
    m = _trained(X, yf, ym)
    r = m.holdout_metrics(X, yf, ym, min_auc=0.55, min_corr=0.05)
    assert r["corr_computed"] is True
    assert float(r["corr"]) >= 0.05
    assert r["passed"] is True


def test_validate_sets_deployable_only_on_pass() -> None:
    X, yf, ym = _synth(6000, 150, signal=True, seed=5)
    m = _trained(X, yf, ym)
    m.validate(X, yf, ym, min_auc=0.55, min_corr=0.05)
    assert m.is_deployable is True
    # an impossible floor must revoke it
    m2 = _trained(X, yf, ym)
    m2.validate(X, yf, ym, min_auc=0.55, min_corr=0.999)
    assert m2.is_deployable is False


def test_metrics_report_the_fill_count_used() -> None:
    """Callers need to see how much evidence backs the number."""
    X, yf, ym = _synth(5000, 90, signal=True, seed=6)
    m = _healthy_model()
    r = m.holdout_metrics(X, yf, ym, min_auc=0.55, min_corr=0.05)
    assert r["n_fills"] == 90


def test_threshold_is_far_below_the_old_hard_coded_100() -> None:
    """The old bound was unreachable for a sparse live buffer."""
    assert _MIN_FILLS_FOR_MARKOUT_CV <= 30
    assert _MIN_FILLS_FOR_MARKOUT_CV >= 10, "too few fills to trust a correlation"


def test_correlation_is_deterministic_for_a_fixed_seed() -> None:
    X, yf, ym = _synth(4000, 100, signal=True, seed=7)
    m = _trained(X, yf, ym)
    a = m.holdout_metrics(X, yf, ym, seed=42)["corr"]
    b = m.holdout_metrics(X, yf, ym, seed=42)["corr"]
    assert a == pytest.approx(b)
