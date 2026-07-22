"""Compare two StrategyProfiles on the same journal window (eval harness).

Pure helpers used by scripts/compare_strategies.py. Does not change strategy
math — only runs the existing replay path twice and diffs T1-01 metrics.

Supports an optional holdout slice by timestamp fraction so candidates can be
scored out-of-sample relative to a tuning window (anti-overfitting aid).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta
from polymaker.metrics.analyze import MetricsReport, analyze
from polymaker.replay import ReplayResult, filter_rows_for_tokens, load_journal, run_replay


METRIC_KEYS = (
    "realized_spread_usdc",
    "inventory_drift_abs_peak",
    "n_quote",
    "n_cancel",
    "n_fill",
    "n_mark",
)


@dataclass(frozen=True)
class CompareResult:
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    delta: dict[str, Any]
    baseline_replay: dict[str, Any]
    candidate_replay: dict[str, Any]
    window: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "delta": self.delta,
            "baseline_replay": self.baseline_replay,
            "candidate_replay": self.candidate_replay,
        }


def slice_journal_rows(
    rows: list[dict[str, Any]],
    *,
    holdout_frac: float = 0.0,
    use_holdout: bool = False,
    split: str = "time",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Optionally keep only the first or last fraction of the journal.

    holdout_frac in (0, 1):
      split='time'   → cut by timestamp span (can be thin if activity is front-loaded)
      split='events' → cut by event count (guarantees holdout has ~frac of rows)
    use_holdout=False → tuning/in-sample (first 1-holdout_frac).
    use_holdout=True  → OOS holdout (last holdout_frac).
    holdout_frac<=0   → full journal.
    """
    if not rows or holdout_frac <= 0.0:
        ts0 = float(rows[0].get("ts") or 0.0) if rows else 0.0
        ts1 = float(rows[-1].get("ts") or 0.0) if rows else 0.0
        return rows, {
            "mode": "full",
            "split": split,
            "n_events": len(rows),
            "ts_start": ts0,
            "ts_end": ts1,
            "holdout_frac": 0.0,
        }

    frac = min(max(float(holdout_frac), 0.0), 0.95)
    if split == "events":
        cut_i = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - frac)))))
        if use_holdout:
            sliced = rows[cut_i:]
            mode = "holdout_events"
        else:
            sliced = rows[:cut_i]
            mode = "tune_events"
        if not sliced:
            sliced = rows
            mode = f"{mode}_fallback_full"
        return sliced, {
            "mode": mode,
            "split": "events",
            "n_events": len(sliced),
            "n_events_full": len(rows),
            "cut_index": cut_i,
            "ts_start": float(sliced[0].get("ts") or 0.0),
            "ts_end": float(sliced[-1].get("ts") or 0.0),
            "holdout_frac": frac,
        }

    ts_vals = [float(r.get("ts") or 0.0) for r in rows]
    t_min, t_max = min(ts_vals), max(ts_vals)
    span = t_max - t_min
    if span <= 0:
        return rows, {
            "mode": "full_degenerate_ts",
            "split": "time",
            "n_events": len(rows),
            "ts_start": t_min,
            "ts_end": t_max,
            "holdout_frac": frac,
        }

    cut = t_min + span * (1.0 - frac)
    if use_holdout:
        sliced = [r for r in rows if float(r.get("ts") or 0.0) >= cut]
        mode = "holdout"
    else:
        sliced = [r for r in rows if float(r.get("ts") or 0.0) < cut]
        mode = "tune"
    if not sliced:
        sliced = rows
        mode = f"{mode}_fallback_full"
    return sliced, {
        "mode": mode,
        "split": "time",
        "n_events": len(sliced),
        "n_events_full": len(rows),
        "ts_start": float(sliced[0].get("ts") or 0.0),
        "ts_end": float(sliced[-1].get("ts") or 0.0),
        "cut_ts": cut,
        "holdout_frac": frac,
    }


def write_sliced_journal(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return path


def _replay_summary(result: ReplayResult) -> dict[str, Any]:
    return {
        "events_read": result.events_read,
        "events_applied": result.events_applied,
        "recomputes": result.recomputes,
        "n_quote": result.n_quote,
        "n_cancel": result.n_cancel,
        "n_mark": result.n_mark,
        "metrics_path": result.metrics_path,
    }


def _report_metrics(rep: MetricsReport) -> dict[str, Any]:
    d = rep.as_dict()
    out: dict[str, Any] = {k: d.get(k) for k in METRIC_KEYS}
    out["markout_mean"] = d.get("markout_mean") or {}
    out["markout_n"] = d.get("markout_n") or {}
    out["reward_accrual_usdc"] = d.get("reward_accrual_usdc") or {}
    out["rebate_pool_daily_usdc"] = d.get("rebate_pool_daily_usdc") or {}
    out["inventory_net_end"] = d.get("inventory_net_end") or {}
    return out


def _delta(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for k in METRIC_KEYS:
        b, c = baseline.get(k), candidate.get(k)
        if isinstance(b, (int, float)) and isinstance(c, (int, float)):
            delta[k] = round(float(c) - float(b), 6)
    # markout horizons
    bm = baseline.get("markout_mean") or {}
    cm = candidate.get("markout_mean") or {}
    if isinstance(bm, dict) and isinstance(cm, dict):
        keys = sorted(set(bm) | set(cm))
        delta["markout_mean"] = {
            k: round(float(cm.get(k, 0.0)) - float(bm.get(k, 0.0)), 6) for k in keys
        }
    br = baseline.get("reward_accrual_usdc") or {}
    cr = candidate.get("reward_accrual_usdc") or {}
    if isinstance(br, dict) and isinstance(cr, dict):
        keys = sorted(set(br) | set(cr))
        delta["reward_accrual_usdc"] = {
            k: round(float(cr.get(k, 0.0)) - float(br.get(k, 0.0)), 6) for k in keys
        }
    return delta


def compare_profiles(
    journal_path: Path,
    meta: MarketMeta,
    baseline: StrategyProfile,
    candidate: StrategyProfile,
    out_dir: Path,
    *,
    holdout_frac: float = 0.0,
    use_holdout: bool = False,
    split: str = "time",
) -> CompareResult:
    """Replay baseline and candidate on the same (optionally sliced) journal."""
    rows = load_journal(journal_path)
    n_unfiltered = len(rows)
    # Restrict to this market's tokens before holdout cuts (multi-market tapes).
    yes_id = meta.yes.token_id
    no_id = meta.no.token_id
    if yes_id not in ("yes-token", "") and no_id not in ("no-token", ""):
        filtered = filter_rows_for_tokens(rows, yes_token=yes_id, no_token=no_id)
        if filtered:
            rows = filtered
    sliced, window = slice_journal_rows(
        rows, holdout_frac=holdout_frac, use_holdout=use_holdout, split=split
    )
    window = {
        **window,
        "n_events_unfiltered": n_unfiltered,
        "n_events_market": len(rows),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    sliced_path = write_sliced_journal(sliced, out_dir / "journal_window.jsonl")

    base_metrics = out_dir / "metrics_baseline.jsonl"
    cand_metrics = out_dir / "metrics_candidate.jsonl"
    # Wipe prior runs so analyze does not see stale lines if logger appends.
    for p in (base_metrics, cand_metrics):
        if p.exists():
            p.unlink()

    r_base = run_replay(sliced_path, meta, baseline, base_metrics)
    r_cand = run_replay(sliced_path, meta, candidate, cand_metrics)
    m_base = _report_metrics(analyze(base_metrics))
    m_cand = _report_metrics(analyze(cand_metrics))
    return CompareResult(
        baseline=m_base,
        candidate=m_cand,
        delta=_delta(m_base, m_cand),
        baseline_replay=_replay_summary(r_base),
        candidate_replay=_replay_summary(r_cand),
        window=window,
    )


def profile_from_overrides(
    base: StrategyProfile | None = None,
    overrides: dict[str, Any] | None = None,
) -> StrategyProfile:
    profile = base or StrategyProfile()
    return profile.with_overrides(overrides or {})


def load_named_profile(
    name: str,
    *,
    config_dir: str | Path = "config",
    overrides: dict[str, Any] | None = None,
) -> StrategyProfile:
    """Load a named StrategyProfile from strategy.toml (no .env required)."""
    from polymaker.config import Config

    cfg = Config.load(config_dir, load_env=False)
    if name not in cfg.profiles:
        known = ", ".join(sorted(cfg.profiles)) or "(none)"
        raise KeyError(f"unknown profile {name!r}; known: {known}")
    return profile_from_overrides(cfg.profiles[name], overrides)
