"""Honest out-of-sample evaluation of the fill-quality gate.

Answers one question with intervals instead of point estimates:

    Does gating fills on the model beat NOT gating, and does it beat a random
    gate that takes the same fraction of fills?

Two protocols are reported:

``few_clusters``
    Replicates the shipped report's split: train on all assets except the N
    largest by fill count, test on those N. Fast, but the interval is wide
    because N assets is N independent observations.

``leave_one_asset_out``
    Every asset takes a turn as the test set. Uses all assets as evidence, so
    the cluster bootstrap has real resolution. This is the protocol to steer
    on.

Usage
-----
    uv run python scripts/eval_fill_model.py --cache /tmp/fill_cache_live.pkl
    uv run python scripts/eval_fill_model.py --journal journal/paper.jsonl

Exit code is 1 when the model fails to beat the matched-retention control, so
this can gate a retrain in CI.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polymaker.strategy.fill_eval import (  # noqa: E402
    EvalReport,
    brier_score,
    cluster_bootstrap_ci,
    compare_to_control,
    evaluate_gate,
    expected_calibration_error,
    random_gate_control,
    roc_auc,
)

_HGB_KW: dict[str, Any] = dict(
    max_iter=100, max_depth=4, min_samples_leaf=10,
    early_stopping=False, random_state=42,
)


def _fit_good_fill(X: np.ndarray, y_good: np.ndarray) -> Any:
    """P(good|fill) classifier, class-balanced (fills are rare and skewed)."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    if np.unique(y_good).size < 2:
        return None
    pos = float(y_good.sum())
    neg = float(y_good.size - pos)
    # Balanced sample weights: without this the trees chase the majority prior.
    w = np.where(y_good > 0, neg / max(pos, 1.0), 1.0)
    clf = HistGradientBoostingClassifier(**_HGB_KW)
    clf.fit(X, y_good, sample_weight=w)
    return clf


def load_fills(
    *, cache: Path | None, journal: list[Path] | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (X_fills, markout_fills, asset_per_fill)."""
    if cache is not None:
        store, stats = pickle.loads(cache.read_bytes())
        arrays = store.raw_arrays()
        if arrays is None:
            raise SystemExit(f"cache {cache} has no samples")
        X, y_fill, y_mk = arrays
        meta = stats.get("fill_meta") or []
        if not meta:
            raise SystemExit(f"cache {cache} has no fill_meta (no asset labels)")
        n = len(meta)
        if not bool((y_fill[:n] == 1).all()):
            raise SystemExit(
                "cache layout unexpected: the first len(fill_meta) rows are not "
                "all fills, so asset labels cannot be aligned"
            )
        return X[:n], y_mk[:n], np.array([m["asset"] for m in meta])

    assert journal is not None
    from train_fill_model import build_training_store  # type: ignore[import-not-found]

    store, stats = build_training_store(journal)
    arrays = store.raw_arrays()
    if arrays is None:
        raise SystemExit("journal produced no samples")
    X, y_fill, y_mk = arrays
    groups = np.array(stats["groups"])
    m = y_fill > 0
    return X[m], y_mk[m], groups[m]


def _gate_eval(
    y_score: np.ndarray,
    markout: np.ndarray,
    assets: np.ndarray,
    retention: float,
    *,
    seed: int,
) -> tuple[Any, dict[str, Any]]:
    """Take the top ``retention`` fraction by score; score vs random control."""
    k = max(1, int(round(retention * y_score.size)))
    thresh_idx = np.argsort(-y_score, kind="stable")[:k]
    taken = np.zeros(y_score.size, dtype=bool)
    taken[thresh_idx] = True
    res = evaluate_gate(markout, taken, assets, seed=seed)
    ctrl_wr, _lo, ctrl_hi = random_gate_control(
        markout, assets, res.retention, seed=seed
    )
    return res, compare_to_control(res, ctrl_wr, ctrl_hi)


def run(
    X: np.ndarray,
    markout: np.ndarray,
    assets: np.ndarray,
    *,
    protocol: str,
    n_holdout: int,
    retention: float,
    seed: int,
) -> EvalReport:
    y_good = (markout > 0).astype(np.float64)
    counts = Counter(assets.tolist())
    uniq = [a for a, _ in counts.most_common()]

    rep = EvalReport(
        protocol=protocol,
        n_samples=int(X.shape[0]),
        n_assets=len(uniq),
        n_fills=int(X.shape[0]),
    )
    # Fail CLOSED: an unevaluable model must never present an empty verdict,
    # which a caller could misread as "no objection".
    rep.verdict = {
        "beats_control": False,
        "reason": "not evaluated",
    }

    # ── baseline: take every fill (no gate) ──────────────────────────────
    base_wr, base_lo, base_hi, n_cl = cluster_bootstrap_ci(y_good, assets, seed=seed)
    rep.baseline = {
        "win_rate": round(base_wr, 4),
        "wr_ci95": (round(base_lo, 4), round(base_hi, 4)),
        "mean_markout": round(float(markout.mean()), 6),
        "n_fills": int(markout.size),
        "n_clusters": n_cl,
    }

    if np.unique(y_good).size < 2:
        rep.verdict = {
            "beats_control": False,
            "reason": (
                "all fills share one outcome label "
                f"({'all winners' if y_good.all() else 'all losers'}); "
                "no gate can be validated on a single-class target"
            ),
        }
        rep.notes.append("single-class markout labels — nothing to discriminate")
        return rep

    oof_score = np.full(X.shape[0], np.nan)

    if protocol == "few_clusters":
        test_assets = set(uniq[:n_holdout])
        te = np.array([i for i, a in enumerate(assets) if a in test_assets])
        tr = np.array([i for i, a in enumerate(assets) if a not in test_assets])
        clf = _fit_good_fill(X[tr], y_good[tr])
        if clf is None:
            rep.verdict = {
                "beats_control": False,
                "reason": "training split had a single class; no model fit",
            }
            rep.notes.append("training split had a single class; no model fit")
            return rep
        oof_score[te] = clf.predict_proba(X[te])[:, 1]
        eval_idx = te
        rep.notes.append(
            f"held out the {n_holdout} largest assets by fill count "
            f"({len(te)} test fills). Only {n_holdout} independent clusters — "
            "intervals are wide by construction."
        )
    else:  # leave_one_asset_out
        for a in uniq:
            te = np.array([i for i, x in enumerate(assets) if x == a])
            tr = np.array([i for i, x in enumerate(assets) if x != a])
            if te.size == 0 or tr.size == 0:
                continue
            clf = _fit_good_fill(X[tr], y_good[tr])
            if clf is None:
                continue
            oof_score[te] = clf.predict_proba(X[te])[:, 1]
        eval_idx = np.flatnonzero(np.isfinite(oof_score))
        rep.notes.append(
            f"every one of {len(uniq)} assets served as holdout once; scores "
            "are strictly out-of-fold"
        )

    if eval_idx.size == 0:
        rep.verdict = {
            "beats_control": False,
            "reason": "no out-of-fold scores could be produced",
        }
        rep.notes.append("no evaluable samples")
        return rep

    s = oof_score[eval_idx]
    mk = markout[eval_idx]
    ag = assets[eval_idx]
    yg = y_good[eval_idx]

    rep.good_fill_auc = roc_auc(yg, s)
    rep.brier = brier_score(yg, s)
    rep.ece = expected_calibration_error(yg, s)

    res, verdict = _gate_eval(s, mk, ag, retention, seed=seed)
    rep.gated = res.as_dict()
    rep.control = {
        "matched_retention": round(res.retention, 4),
        "control_wr": verdict["control_wr"],
        "control_wr_hi": verdict["control_wr_hi"],
    }
    rep.verdict = verdict
    return rep


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--cache", type=Path, help="pickled (FillTrainingStore, stats)")
    src.add_argument("--journal", type=Path, action="append", help="raw journal JSONL")
    p.add_argument("--n-holdout", type=int, default=4)
    p.add_argument("--retention", type=float, default=0.44,
                   help="fraction of fills the gate is allowed to take")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--protocol", default="both",
                   choices=["few_clusters", "leave_one_asset_out", "both"])
    p.add_argument("--json-out", type=Path)
    args = p.parse_args()

    X, mk, assets = load_fills(cache=args.cache, journal=args.journal)
    protocols = (
        ["few_clusters", "leave_one_asset_out"]
        if args.protocol == "both"
        else [args.protocol]
    )
    reports = [
        run(X, mk, assets, protocol=proto, n_holdout=args.n_holdout,
            retention=args.retention, seed=args.seed)
        for proto in protocols
    ]
    out = {"reports": [r.as_dict() for r in reports]}
    print(json.dumps(out, indent=2, default=str))
    if args.json_out:
        args.json_out.write_text(json.dumps(out, indent=2, default=str))

    decisive = reports[-1]
    passed = bool(decisive.verdict.get("beats_control"))
    print(
        f"\nVERDICT ({decisive.protocol}): "
        f"{'BEATS' if passed else 'DOES NOT BEAT'} the matched-retention "
        f"random control.",
        file=sys.stderr,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
