"""Quantitative Edge evidence harness (Tier-1 eval infra).

Compares baseline vs candidate profiles on the same journal with the
evidence standard required by the Quantitative Edge deep-dive:

1. Calibration metrics (Brier / log-loss / ECE) — not accuracy
2. EV per quote net of adverse selection
3. Out-of-sample holdout (tune vs holdout windows)
4. Bootstrap CI + paired significance on chunked EV deltas

Does not modify strategy math — only runs the existing replay/compare path.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta
from polymaker.replay import load_journal
from polymaker.replay.compare import (
    METRIC_KEYS,
    compare_profiles,
    slice_journal_rows,
    write_sliced_journal,
)
from polymaker.strategy.calibration import (
    bootstrap_confidence_interval,
    paired_significance_test,
)


# Techniques tracked by this loop. "module" = pure code exists;
# "wired" = on a live/replay quote path; "evidence" = passed OOS+CI gate.
TECHNIQUE_INVENTORY: tuple[dict[str, str], ...] = (
    {"id": "microprice", "module": "yes", "wired": "yes", "evidence": "mixed"},  # MSE Newsom; EV micro5=no
    {"id": "ewma_fv_vol", "module": "yes", "wired": "yes", "evidence": "partial"},
    {"id": "flow_nudge_fv", "module": "yes", "wired": "yes", "evidence": "no"},
    {"id": "kalman_mid", "module": "yes", "wired": "intel-only", "evidence": "no"},
    {"id": "signal_blend_calibration", "module": "yes", "wired": "no", "evidence": "no"},
    {"id": "avellaneda_stoikov", "module": "yes", "wired": "opt-in", "evidence": "no"},
    {"id": "kelly_fractional", "module": "yes", "wired": "opt-in", "evidence": "no"},
    {"id": "kyle_lambda", "module": "yes", "wired": "fed", "evidence": "mixed"},
    {"id": "vpin", "module": "yes", "wired": "fed", "evidence": "no"},
    {"id": "garch_vol", "module": "yes", "wired": "no", "evidence": "no"},
    {"id": "ofi_skew", "module": "yes", "wired": "fed", "evidence": "no"},
    {"id": "covariance_sizing", "module": "yes", "wired": "no", "evidence": "no"},
    {"id": "markout_toxicity", "module": "yes", "wired": "yes", "evidence": "mixed"},
)


@dataclass(frozen=True)
class QuantEdgeEval:
    """Full + OOS + significance package for one baseline/candidate pair."""

    inventory: tuple[dict[str, str], ...]
    full: dict[str, Any]
    tune: dict[str, Any]
    holdout: dict[str, Any]
    significance: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "inventory": list(self.inventory),
            "full": self.full,
            "tune": self.tune,
            "holdout": self.holdout,
            "significance": self.significance,
            "verdict": self.verdict,
        }


def _window_compare(
    journal: Path,
    meta: MarketMeta,
    baseline: StrategyProfile,
    candidate: StrategyProfile,
    out_dir: Path,
    *,
    holdout_frac: float,
    use_holdout: bool,
    split: str,
    fill_mode: str = "conservative",
) -> dict[str, Any]:
    result = compare_profiles(
        journal,
        meta,
        baseline,
        candidate,
        out_dir,
        holdout_frac=holdout_frac,
        use_holdout=use_holdout,
        split=split,
        fill_mode=fill_mode,
    )
    d = result.as_dict()
    # Surface the evidence-standard scalars explicitly.
    cal_keys = ("brier_score", "log_loss", "expected_calibration_error", "ev_per_quote_usdc")
    return {
        "window": d["window"],
        "baseline": {k: d["baseline"].get(k) for k in METRIC_KEYS},
        "candidate": {k: d["candidate"].get(k) for k in METRIC_KEYS},
        "delta": {k: d["delta"].get(k) for k in METRIC_KEYS},
        "calibration_delta": {k: d["delta"].get(k) for k in cal_keys},
        "baseline_replay": d["baseline_replay"],
        "candidate_replay": d["candidate_replay"],
    }


def _chunk_ev_series(
    journal: Path,
    meta: MarketMeta,
    baseline: StrategyProfile,
    candidate: StrategyProfile,
    out_dir: Path,
    *,
    n_chunks: int,
    split: str,
    fill_mode: str = "conservative",
) -> tuple[list[float], list[float]]:
    """Replay N sequential event-chunks; return baseline/candidate EV series."""
    rows = load_journal(journal)
    if len(rows) < n_chunks * 2 or n_chunks < 2:
        return [], []

    chunk_size = len(rows) // n_chunks
    base_evs: list[float] = []
    cand_evs: list[float] = []
    for i in range(n_chunks):
        start = i * chunk_size
        end = (i + 1) * chunk_size if i < n_chunks - 1 else len(rows)
        chunk = rows[start:end]
        if len(chunk) < 10:
            continue
        chunk_dir = out_dir / f"chunk_{i}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = write_sliced_journal(chunk, chunk_dir / "journal.jsonl")
        cmp = compare_profiles(
            chunk_path,
            meta,
            baseline,
            candidate,
            chunk_dir / "cmp",
            holdout_frac=0.0,
            use_holdout=False,
            split=split,
            fill_mode=fill_mode,
        )
        base_evs.append(float(cmp.baseline.get("ev_per_quote_usdc") or 0.0))
        cand_evs.append(float(cmp.candidate.get("ev_per_quote_usdc") or 0.0))
    return base_evs, cand_evs


def evaluate_quant_edge(
    journal: Path,
    meta: MarketMeta,
    baseline: StrategyProfile,
    candidate: StrategyProfile,
    out_dir: Path,
    *,
    holdout_frac: float = 0.3,
    split: str = "events",
    n_chunks: int = 5,
    alpha: float = 0.05,
    fill_mode: str = "conservative",
) -> QuantEdgeEval:
    """Run full / tune / holdout compares + chunked significance on EV.

    fill_mode=conservative is the promotion default. base/optimistic are
    diagnostics when conservative yields n_fill≈0 (queue-ahead blocks fills).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    full = _window_compare(
        journal, meta, baseline, candidate, out_dir / "full",
        holdout_frac=0.0, use_holdout=False, split=split, fill_mode=fill_mode,
    )
    tune = _window_compare(
        journal, meta, baseline, candidate, out_dir / "tune",
        holdout_frac=holdout_frac, use_holdout=False, split=split,
        fill_mode=fill_mode,
    )
    holdout = _window_compare(
        journal, meta, baseline, candidate, out_dir / "holdout",
        holdout_frac=holdout_frac, use_holdout=True, split=split,
        fill_mode=fill_mode,
    )

    base_evs, cand_evs = _chunk_ev_series(
        journal, meta, baseline, candidate, out_dir / "chunks",
        n_chunks=n_chunks, split=split, fill_mode=fill_mode,
    )
    deltas = [c - b for b, c in zip(base_evs, cand_evs)]
    if deltas:
        mean, lo, hi = bootstrap_confidence_interval(deltas, n_resamples=1000, seed=7)
        sig = paired_significance_test(base_evs, cand_evs, alpha=alpha)
        significance = {
            "n_chunks": len(deltas),
            "baseline_ev_chunks": base_evs,
            "candidate_ev_chunks": cand_evs,
            "delta_ev_chunks": deltas,
            "bootstrap_mean_delta": mean,
            "bootstrap_ci_lower": lo,
            "bootstrap_ci_upper": hi,
            "paired_mean_delta": sig.mean_delta,
            "paired_t": sig.t_statistic,
            "paired_p": sig.p_value,
            "is_significant": sig.is_significant,
            "paired_ci_lower": sig.ci_lower,
            "paired_ci_upper": sig.ci_upper,
            "alpha": alpha,
        }
    else:
        significance = {
            "n_chunks": 0,
            "baseline_ev_chunks": [],
            "candidate_ev_chunks": [],
            "delta_ev_chunks": [],
            "bootstrap_mean_delta": 0.0,
            "bootstrap_ci_lower": 0.0,
            "bootstrap_ci_upper": 0.0,
            "paired_mean_delta": 0.0,
            "paired_t": 0.0,
            "paired_p": 1.0,
            "is_significant": False,
            "paired_ci_lower": 0.0,
            "paired_ci_upper": 0.0,
            "alpha": alpha,
            "reason": "insufficient_events_for_chunks",
        }

    # OOS replication: holdout EV delta sign matches full, and CI excludes 0
    # only when significant — otherwise "not a finding".
    full_ev_delta = float(full["delta"].get("ev_per_quote_usdc") or 0.0)
    hold_ev_delta = float(holdout["delta"].get("ev_per_quote_usdc") or 0.0)
    oos_sign_match = (full_ev_delta == 0.0 and hold_ev_delta == 0.0) or (
        full_ev_delta * hold_ev_delta > 0.0
    )
    ci_lo = float(significance.get("bootstrap_ci_lower") or 0.0)
    ci_hi = float(significance.get("bootstrap_ci_upper") or 0.0)
    # Degenerate CI (all chunk deltas identical → lo==hi) is not evidence.
    ci_width = abs(ci_hi - ci_lo)
    ci_excludes_zero = (
        significance["n_chunks"] >= 2
        and ci_width > 1e-12
        and (ci_lo > 0.0 or ci_hi < 0.0)
    )
    finding = bool(
        oos_sign_match
        and significance.get("is_significant")
        and ci_excludes_zero
        and hold_ev_delta > 0.0
    )
    n_fill_base = int(full.get("baseline", {}).get("n_fill") or 0)
    n_fill_cand = int(full.get("candidate", {}).get("n_fill") or 0)
    promotion_eligible = bool(finding and fill_mode == "conservative")
    verdict = {
        "oos_sign_match": oos_sign_match,
        "ci_excludes_zero": ci_excludes_zero,
        "is_significant": bool(significance.get("is_significant")),
        "full_ev_delta": round(full_ev_delta, 8),
        "holdout_ev_delta": round(hold_ev_delta, 8),
        "finding": finding,
        "fill_mode": fill_mode,
        "n_fill_baseline": n_fill_base,
        "n_fill_candidate": n_fill_cand,
        "promotion_eligible": promotion_eligible,
        "note": (
            "finding=true only when OOS EV improves, paired test is significant, "
            "and bootstrap CI excludes zero; promotion_eligible additionally "
            "requires fill_mode=conservative (base/optimistic are diagnostic)"
        ),
    }

    # Touch slice helper so thin journals still exercise holdout metadata.
    _rows = load_journal(journal)
    _, _meta = slice_journal_rows(_rows, holdout_frac=holdout_frac, use_holdout=True, split=split)
    verdict["holdout_meta"] = _meta

    return QuantEdgeEval(
        inventory=TECHNIQUE_INVENTORY,
        full=full,
        tune=tune,
        holdout=holdout,
        significance=significance,
        verdict=verdict,
    )


def write_report(result: QuantEdgeEval, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n")
    return path


def evaluate_in_temp(
    journal: Path,
    meta: MarketMeta,
    baseline: StrategyProfile,
    candidate: StrategyProfile,
    **kwargs: Any,
) -> QuantEdgeEval:
    """Convenience wrapper that uses a temporary output directory."""
    with tempfile.TemporaryDirectory(prefix="quant_edge_") as td:
        return evaluate_quant_edge(
            journal, meta, baseline, candidate, Path(td), **kwargs
        )
