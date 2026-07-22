#!/usr/bin/env python3
"""Validate a knob candidate on full tape + OOS holdout (anti-overfitting).

Runs the same sweep twice (full window, then holdout slice) and prints both
ledgers. A candidate that only wins in-sample is flagged.

Usage:
  uv run python scripts/validate_knob_candidate.py \\
      --journal livecfg/journal/paper.jsonl \\
      --config-dir livecfg --baseline-profile live-tiny \\
      --knob trend_vol_ratio --values 2,5,8 \\
      --yes-token ... --no-token ... --condition-id ... --tick-size 0.001
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import compare_profiles, load_named_profile, profile_from_overrides


def _parse_values(raw: str, knob: str) -> list[Any]:
    out: list[Any] = []
    sample = StrategyProfile().model_dump()[knob]
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if isinstance(sample, bool):
            out.append(part.lower() in ("1", "true", "yes"))
        elif isinstance(sample, int) and not isinstance(sample, bool):
            out.append(int(float(part)))
        else:
            out.append(float(part))
    return out


def _run_window(
    *,
    journal: Path,
    meta: MarketMeta,
    baseline: StrategyProfile,
    knob: str,
    values: list[Any],
    holdout_frac: float,
    use_holdout: bool,
    split: str,
) -> dict[str, Any]:
    rows = []
    window_meta: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="validate_") as td:
        root = Path(td)
        for i, val in enumerate(values):
            cand = profile_from_overrides(baseline, {knob: val})
            result = compare_profiles(
                journal,
                meta,
                baseline,
                cand,
                root / f"v{i}",
                holdout_frac=holdout_frac,
                use_holdout=use_holdout,
                split=split,
            )
            if not window_meta:
                window_meta = dict(result.window)
            rows.append({
                "candidate_value": val,
                "delta_n_quote": result.delta.get("n_quote"),
                "delta_n_cancel": result.delta.get("n_cancel"),
                "candidate_n_quote": result.candidate.get("n_quote"),
                "baseline_n_quote": result.baseline.get("n_quote"),
            })
    return {"window": window_meta, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--baseline-profile", default=None)
    ap.add_argument("--knob", required=True)
    ap.add_argument("--values", required=True)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--split", choices=("time", "events"), default="events",
                    help="Holdout cut by timestamp span or event count (default events)")
    ap.add_argument("--yes-token", default="yes-token")
    ap.add_argument("--no-token", default="no-token")
    ap.add_argument("--condition-id", default="0xreplay")
    ap.add_argument("--tick-size", type=float, default=0.01)
    # Optional "improvement" direction for auto-flag: lower n_quote/cancel = less churn
    ap.add_argument("--prefer", choices=("lower_quotes", "higher_quotes"), default="lower_quotes")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=NO_JOURNAL path={journal}", file=sys.stderr)
        return 2
    if args.knob not in StrategyProfile().model_dump():
        print(f"status=BAD_KNOB {args.knob}", file=sys.stderr)
        return 2

    baseline = (
        load_named_profile(args.baseline_profile, config_dir=args.config_dir)
        if args.baseline_profile
        else StrategyProfile()
    )
    values = _parse_values(args.values, args.knob)
    meta = MarketMeta(
        condition_id=args.condition_id,
        question="validate",
        slug="validate",
        tokens=(TokenMeta(args.yes_token, "Yes"), TokenMeta(args.no_token, "No")),
        tick_size=args.tick_size,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )

    full = _run_window(
        journal=journal, meta=meta, baseline=baseline, knob=args.knob, values=values,
        holdout_frac=0.0, use_holdout=False, split=args.split,
    )
    hold = _run_window(
        journal=journal, meta=meta, baseline=baseline, knob=args.knob, values=values,
        holdout_frac=args.holdout_frac, use_holdout=True, split=args.split,
    )

    # Pick best non-baseline value on full window by prefer direction
    base_val = getattr(baseline, args.knob)
    candidates = [r for r in full["rows"] if r["candidate_value"] != base_val]
    def score(r: dict[str, Any]) -> float:
        dq = float(r.get("delta_n_quote") or 0.0)
        return -dq if args.prefer == "lower_quotes" else dq
    best = max(candidates, key=score) if candidates else None
    hold_row = None
    if best is not None:
        hold_row = next(
            (r for r in hold["rows"] if r["candidate_value"] == best["candidate_value"]),
            None,
        )

    oos_ok = False
    thin_holdout = False
    if hold_row is not None:
        hold_n = int(hold_row.get("baseline_n_quote") or 0)
        thin_holdout = hold_n < 20
        dq_full = float(best["delta_n_quote"] or 0.0)
        dq_hold = float(hold_row["delta_n_quote"] or 0.0)
        if args.prefer == "lower_quotes":
            oos_ok = (dq_full < 0) and (dq_hold < 0) and not thin_holdout
        else:
            oos_ok = (dq_full > 0) and (dq_hold > 0) and not thin_holdout

    payload = {
        "knob": args.knob,
        "baseline_profile": args.baseline_profile,
        "baseline_value": base_val,
        "prefer": args.prefer,
        "full": full,
        "holdout": hold,
        "best_full": best,
        "best_on_holdout": hold_row,
        "oos_replicated": oos_ok,
        "thin_holdout": thin_holdout,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    bval = best["candidate_value"] if best else None
    bdq = best["delta_n_quote"] if best else None
    hdq = hold_row["delta_n_quote"] if hold_row else None
    print(
        f"status=OK knob={args.knob} best={bval} full_dn_quote={bdq} "
        f"holdout_dn_quote={hdq} oos_replicated={oos_ok} thin_holdout={thin_holdout}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
