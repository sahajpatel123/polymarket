"""Honest out-of-sample evaluation for the fill model.

Why this module exists
----------------------
The previous evaluation reported a single point estimate ("75.3% win rate,
AUC 0.880") from a leave-assets-out split with 4 test assets. Three things
make that number unsafe to steer on:

1. **No uncertainty.** Fills cluster hard within an asset, so 718 test fills
   from 4 assets carry roughly 4 independent observations, not 718. A point
   estimate with no interval cannot tell an improvement from noise.
2. **No control.** A gate that keeps 52% of fills must be compared against a
   *random* gate keeping 52%, otherwise part of the "win rate lift" is just
   the mechanical effect of taking fewer fills.
3. **Ranking-only metrics.** AUC says the ordering is good; it says nothing
   about whether the probabilities are calibrated, which is what the win-rate
   governor's consensus floor actually consumes.

Everything here is deliberately conservative: it is designed to be able to
report "this model is NOT better", because a measuring stick that can only
report success is not a measuring stick.

All functions are pure and offline. No engine imports, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "GateResult",
    "EvalReport",
    "grouped_folds",
    "purged_time_folds",
    "bootstrap_ci",
    "cluster_bootstrap_ci",
    "brier_score",
    "expected_calibration_error",
    "roc_auc",
    "evaluate_gate",
    "random_gate_control",
    "compare_to_control",
]

_EPS = 1e-12


# ── splitting ────────────────────────────────────────────────────────────


def grouped_folds(
    groups: np.ndarray, n_folds: int = 5
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Leave-assets-out folds: no asset appears in both train and test.

    Assets are assigned to folds greedily by descending sample count so folds
    stay balanced even when one asset dominates the journal (which is the
    normal case for thin political markets).
    """
    g = np.asarray(groups)
    uniq, counts = np.unique(g, return_counts=True)
    if uniq.size < 2:
        raise ValueError(
            f"grouped validation needs >=2 distinct groups, got {uniq.size}. "
            "A single-asset journal cannot be validated out-of-sample."
        )
    n_folds = int(min(n_folds, uniq.size))
    # Greedy balanced assignment, deterministic tie-break so equal-count
    # assets never depend on dict/hash ordering.
    order = np.array(sorted(range(uniq.size), key=lambda i: (-counts[i], str(uniq[i]))))
    load = np.zeros(n_folds, dtype=np.int64)
    assign: dict[Any, int] = {}
    for i in order:
        f = int(np.argmin(load))
        assign[uniq[i]] = f
        load[f] += counts[i]
    fold_of = np.array([assign[x] for x in g])
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for f in range(n_folds):
        te = np.flatnonzero(fold_of == f)
        tr = np.flatnonzero(fold_of != f)
        if te.size and tr.size:
            out.append((tr, te))
    return out


def purged_time_folds(
    ts: np.ndarray, n_folds: int = 4, *, embargo_s: float = 60.0
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Forward-chaining time folds with an embargo gap after each train block.

    The label horizon is 30s forward markout, so a train sample whose label
    resolves *inside* the test window leaks. ``embargo_s`` must be >= the
    label horizon. Train is always strictly before test (no look-ahead).
    """
    t = np.asarray(ts, dtype=np.float64)
    n = t.size
    if n < 4:
        return []
    order = np.argsort(t, kind="stable")
    t_sorted = t[order]
    edges = np.linspace(0, n, n_folds + 2, dtype=np.int64)
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for k in range(1, n_folds + 1):
        tr_end, te_end = int(edges[k]), int(edges[k + 1])
        if tr_end <= 0 or te_end <= tr_end:
            continue
        cutoff = t_sorted[tr_end - 1]
        tr_mask = t[order] <= cutoff - embargo_s
        tr = order[tr_mask]
        te = order[tr_end:te_end]
        if tr.size and te.size:
            out.append((tr, te))
    return out


# ── uncertainty ──────────────────────────────────────────────────────────


def bootstrap_ci(
    values: np.ndarray, *, n_boot: int = 2000, alpha: float = 0.05, seed: int = 42
) -> tuple[float, float, float]:
    """(mean, lo, hi) percentile bootstrap CI over independent observations."""
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    if v.size == 1:
        return (float(v[0]), float("nan"), float("nan"))
    rng = np.random.RandomState(seed)
    idx = rng.randint(0, v.size, size=(n_boot, v.size))
    means = v[idx].mean(axis=1)
    return (
        float(v.mean()),
        float(np.percentile(means, 100 * alpha / 2)),
        float(np.percentile(means, 100 * (1 - alpha / 2))),
    )


def cluster_bootstrap_ci(
    values: np.ndarray,
    clusters: np.ndarray,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float, int]:
    """Cluster (block) bootstrap: resample ASSETS, not rows.

    This is the honest interval for fill metrics. Rows within an asset are
    strongly dependent, so a row-level bootstrap understates the interval by
    roughly sqrt(rows_per_asset). Returns (mean, lo, hi, n_clusters).
    """
    v = np.asarray(values, dtype=np.float64)
    c = np.asarray(clusters)
    ok = np.isfinite(v)
    v, c = v[ok], c[ok]
    if v.size == 0:
        return (float("nan"), float("nan"), float("nan"), 0)
    uniq = np.unique(c)
    by: list[np.ndarray] = [v[c == u] for u in uniq]
    if uniq.size < 2:
        return (float(v.mean()), float("nan"), float("nan"), int(uniq.size))
    rng = np.random.RandomState(seed)
    means = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        pick = rng.randint(0, uniq.size, size=uniq.size)
        means[b] = np.concatenate([by[i] for i in pick]).mean()
    return (
        float(v.mean()),
        float(np.percentile(means, 100 * alpha / 2)),
        float(np.percentile(means, 100 * (1 - alpha / 2))),
        int(uniq.size),
    )


# ── metrics ──────────────────────────────────────────────────────────────


def roc_auc(y_true: np.ndarray, score: np.ndarray) -> float:
    """Rank-based ROC AUC. Returns nan when only one class is present."""
    y = np.asarray(y_true, dtype=np.float64)
    s = np.asarray(score, dtype=np.float64)
    pos, neg = y > 0.5, y <= 0.5
    n_pos, n_neg = int(pos.sum()), int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="stable")
    ranks = np.empty_like(s, dtype=np.float64)
    ranks[order] = np.arange(1, s.size + 1, dtype=np.float64)
    # average ranks within ties
    s_sorted = s[order]
    i = 0
    while i < s_sorted.size:
        j = i
        while j + 1 < s_sorted.size and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i : j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def brier_score(y_true: np.ndarray, prob: np.ndarray) -> float:
    """Mean squared error of probabilistic predictions (lower is better)."""
    y = np.asarray(y_true, dtype=np.float64)
    p = np.clip(np.asarray(prob, dtype=np.float64), 0.0, 1.0)
    if y.size == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(
    y_true: np.ndarray, prob: np.ndarray, *, n_bins: int = 10
) -> float:
    """ECE: mean |predicted - observed| across equal-width probability bins.

    The governor maps a consensus score onto an expected win rate. If ECE is
    large, that mapping is fiction even when AUC looks strong.
    """
    y = np.asarray(y_true, dtype=np.float64)
    p = np.clip(np.asarray(prob, dtype=np.float64), 0.0, 1.0)
    if y.size == 0:
        return float("nan")
    # Integer bin index for equal-width bins. np.linspace edges introduce
    # float error (0.6 < 0.6000000000000001), which silently merges bins.
    idx = np.clip((p * n_bins).astype(np.int64), 0, n_bins - 1)
    total = 0.0
    for k in range(n_bins):
        m = idx == k
        if not m.any():
            continue
        total += (m.sum() / y.size) * abs(p[m].mean() - y[m].mean())
    return float(total)


# ── gate evaluation ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateResult:
    """Outcome of applying a selectivity gate to a set of candidate fills."""

    n_total: int
    n_taken: int
    retention: float
    win_rate: float
    wr_lo: float
    wr_hi: float
    mean_markout: float
    mk_lo: float
    mk_hi: float
    total_markout: float
    n_clusters: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "n_total": self.n_total,
            "n_taken": self.n_taken,
            "retention": round(self.retention, 4),
            "win_rate": round(self.win_rate, 4),
            "wr_ci95": (round(self.wr_lo, 4), round(self.wr_hi, 4)),  # type: ignore[dict-item]
            "mean_markout": round(self.mean_markout, 6),
            "mk_ci95": (round(self.mk_lo, 6), round(self.mk_hi, 6)),  # type: ignore[dict-item]
            "total_markout": round(self.total_markout, 4),
            "n_clusters": self.n_clusters,
        }


def evaluate_gate(
    markout: np.ndarray,
    taken: np.ndarray,
    clusters: np.ndarray,
    *,
    seed: int = 42,
    n_boot: int = 2000,
) -> GateResult:
    """Score a boolean gate over fills with cluster-bootstrap intervals.

    ``markout`` is the realized forward markout per fill (label), ``taken`` is
    the gate decision, ``clusters`` is the asset id per fill.
    """
    mk = np.asarray(markout, dtype=np.float64)
    tk = np.asarray(taken).astype(bool)
    cl = np.asarray(clusters)
    n_total = int(mk.size)
    sel_mk, sel_cl = mk[tk], cl[tk]
    n_taken = int(sel_mk.size)
    if n_taken == 0:
        return GateResult(n_total, 0, 0.0, float("nan"), float("nan"),
                          float("nan"), float("nan"), float("nan"),
                          float("nan"), 0.0, 0)
    wins = (sel_mk > 0).astype(np.float64)
    wr, wr_lo, wr_hi, n_cl = cluster_bootstrap_ci(
        wins, sel_cl, n_boot=n_boot, seed=seed
    )
    mkm, mk_lo, mk_hi, _ = cluster_bootstrap_ci(
        sel_mk, sel_cl, n_boot=n_boot, seed=seed
    )
    return GateResult(
        n_total=n_total, n_taken=n_taken,
        retention=n_taken / max(n_total, 1),
        win_rate=wr, wr_lo=wr_lo, wr_hi=wr_hi,
        mean_markout=mkm, mk_lo=mk_lo, mk_hi=mk_hi,
        total_markout=float(sel_mk.sum()), n_clusters=n_cl,
    )


def random_gate_control(
    markout: np.ndarray,
    clusters: np.ndarray,
    retention: float,
    *,
    n_trials: int = 400,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Win rate of a RANDOM gate at matched retention.

    This is the control every model gate must beat. Selecting fewer fills can
    raise win rate purely by variance, so "model WR > baseline WR" is not
    evidence of skill unless it also clears this.

    Returns (mean_wr, p2.5, p97.5) across trials.
    """
    mk = np.asarray(markout, dtype=np.float64)
    n = mk.size
    k = int(round(max(0.0, min(1.0, retention)) * n))
    if n == 0 or k == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.RandomState(seed)
    wins = (mk > 0).astype(np.float64)
    out = np.empty(n_trials, dtype=np.float64)
    for i in range(n_trials):
        out[i] = wins[rng.choice(n, size=k, replace=False)].mean()
    return (
        float(out.mean()),
        float(np.percentile(out, 2.5)),
        float(np.percentile(out, 97.5)),
    )


def compare_to_control(
    model: GateResult, control_wr: float, control_hi: float
) -> dict[str, Any]:
    """Verdict: does the model gate beat a matched-retention random gate?

    ``beats_control`` requires the model's win-rate CI lower bound to exceed
    the control's upper bound — a deliberately strict, non-overlapping test.
    """
    lo = model.wr_lo
    verdict = bool(np.isfinite(lo) and np.isfinite(control_hi) and lo > control_hi)
    return {
        "model_wr": round(model.win_rate, 4),
        "model_wr_lo": None if not np.isfinite(lo) else round(lo, 4),
        "control_wr": None if not np.isfinite(control_wr) else round(control_wr, 4),
        "control_wr_hi": None if not np.isfinite(control_hi) else round(control_hi, 4),
        "lift_vs_control": (
            None if not np.isfinite(control_wr) else round(model.win_rate - control_wr, 4)
        ),
        "beats_control": verdict,
        "reason": (
            "model CI lower bound exceeds control CI upper bound"
            if verdict
            else "model win-rate CI overlaps the matched-retention random control"
        ),
    }


@dataclass
class EvalReport:
    """Full honest report for one model version."""

    protocol: str
    n_samples: int
    n_assets: int
    n_fills: int
    fill_auc: float = float("nan")
    good_fill_auc: float = float("nan")
    brier: float = float("nan")
    ece: float = float("nan")
    baseline: dict[str, Any] = field(default_factory=dict)
    gated: dict[str, Any] = field(default_factory=dict)
    control: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "n_samples": self.n_samples,
            "n_assets": self.n_assets,
            "n_fills": self.n_fills,
            "fill_auc": None if not np.isfinite(self.fill_auc) else round(self.fill_auc, 4),
            "good_fill_auc": (
                None if not np.isfinite(self.good_fill_auc) else round(self.good_fill_auc, 4)
            ),
            "brier": None if not np.isfinite(self.brier) else round(self.brier, 4),
            "ece": None if not np.isfinite(self.ece) else round(self.ece, 4),
            "baseline": self.baseline,
            "gated": self.gated,
            "control": self.control,
            "verdict": self.verdict,
            "notes": self.notes,
        }
