#!/usr/bin/env python3
"""Sweep micro_levels for quote EV (OOS + significance).

Baseline: named profile as-is (default micro_levels=3 on live_scaled).
Candidates: other depths. Prints quant_edge_eval verdict per level.

MSE calibration can prefer depth=5 on some tapes; this script asks whether
that depth also improves EV per quote — the gate for changing the default.

Usage:
  uv run python scripts/micro_levels_ev_sweep.py \\
      --journal livecfg/journal/paper.jsonl \\
      --config-dir livecfg --baseline-profile live_scaled \\
      --yes-token ... --no-token ... --condition-id ... \\
      --levels 3,5 --tick-size 0.001
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import load_named_profile, profile_from_overrides
from polymaker.replay.quant_edge import evaluate_quant_edge


def _meta(args: argparse.Namespace) -> MarketMeta:
    return MarketMeta(
        condition_id=args.condition_id or "0xreplay",
        question="micro-levels-ev",
        slug="micro-levels-ev",
        tokens=(
            TokenMeta(args.yes_token or "yes-token", "Yes"),
            TokenMeta(args.no_token or "no-token", "No"),
        ),
        tick_size=args.tick_size or 0.01,
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--baseline-profile", default="live_scaled")
    ap.add_argument("--levels", default="3,5")
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--n-chunks", type=int, default=4)
    ap.add_argument("--yes-token", default=None)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--report", default="logs/micro_levels_ev_sweep/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    baseline = load_named_profile(args.baseline_profile, config_dir=args.config_dir)
    base_levels = int(baseline.micro_levels)
    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    meta = _meta(args)
    rows: list[dict] = []

    for lvl in levels:
        with tempfile.TemporaryDirectory(prefix=f"micro_ev_{lvl}_") as tmp:
            out = Path(tmp)
            candidate = profile_from_overrides(baseline, {"micro_levels": lvl})
            ev = evaluate_quant_edge(
                journal,
                meta,
                baseline=baseline,
                candidate=candidate,
                out_dir=out,
                holdout_frac=args.holdout_frac,
                split="events",
                n_chunks=args.n_chunks,
            )
            d = ev.as_dict()
            row = {
                "micro_levels": lvl,
                "is_baseline_depth": lvl == base_levels,
                "finding": bool(d["verdict"].get("finding")),
                "holdout_ev_delta": d["verdict"].get("holdout_ev_delta"),
                "full_ev_delta": d["verdict"].get("full_ev_delta"),
                "is_significant": d["verdict"].get("is_significant"),
                "oos_sign_match": d["verdict"].get("oos_sign_match"),
                "paired_p": d["significance"].get("paired_p"),
            }
            rows.append(row)
            print(
                f"level={lvl} finding={row['finding']} "
                f"holdout_dn_ev={row['holdout_ev_delta']} "
                f"p={row['paired_p']} baseline_depth={row['is_baseline_depth']}"
            )

    report = {
        "baseline_micro_levels": base_levels,
        "levels": levels,
        "any_nondefault_finding": any(
            r["finding"] and not r["is_baseline_depth"] for r in rows
        ),
        "note": (
            "finding vs named-profile baseline depth; changing default requires "
            "non-baseline finding=true on multi-market tape"
        ),
        "rows": rows,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"status=OK any_nondefault_finding={report['any_nondefault_finding']} "
        f"report={report_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
