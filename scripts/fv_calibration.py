#!/usr/bin/env python3
"""Calibrate mid vs microprice vs Kalman FV (OOS MSE + significance).

Usage:
  uv run python scripts/fv_calibration.py \\
      --journal livecfg/journal/paper.jsonl \\
      --yes-token ... --no-token ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.fv_calibration import (
    calibrate_fair_value,
    calibrate_fair_value_multi_horizon,
    write_fv_report,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument(
        "--horizons",
        default=None,
        help="Comma-separated horizons for multi-horizon mode (e.g. 5,30,120)",
    )
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--micro-levels", type=int, default=3)
    ap.add_argument("--report", default="logs/fv_calibration/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    if args.horizons:
        hs = tuple(float(x.strip()) for x in args.horizons.split(",") if x.strip())
        multi = calibrate_fair_value_multi_horizon(
            journal,
            yes_token=args.yes_token,
            no_token=args.no_token,
            horizons_s=hs,
            sample_every=args.sample_every,
            holdout_frac=args.holdout_frac,
            micro_levels=args.micro_levels,
        )
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(multi, indent=2, sort_keys=True) + "\n")
        print(f"status=OK mode=multi journal={journal} report={path}")
        print(f"micro_win_horizons={multi.get('micro_win_horizons')} any={multi.get('micro_any_horizon')}")
        for h, v in (multi.get("by_horizon") or {}).items():
            print(
                f"h={h} micro_finding={v.get('micro_finding')} "
                f"mse_mid={v.get('mse_mid')} mse_micro={v.get('mse_micro')}"
            )
        return 0

    rep = calibrate_fair_value(
        journal,
        yes_token=args.yes_token,
        no_token=args.no_token,
        horizon_s=args.horizon_s,
        sample_every=args.sample_every,
        holdout_frac=args.holdout_frac,
        micro_levels=args.micro_levels,
    )
    path = write_fv_report(rep, Path(args.report))
    d = rep.as_dict()
    v = d["verdict"]
    p = d["predictors"]
    print(f"status=OK journal={journal} report={path} n={d['n']}")
    print(
        "mse "
        f"mid={p.get('mid', {}).get('mse')} "
        f"micro={p.get('micro', {}).get('mse')} "
        f"kalman={p.get('kalman', {}).get('mse')} "
        f"blend={p.get('blend', {}).get('mse')}"
    )
    print(
        "verdict "
        f"micro={v.get('micro_finding')} "
        f"kalman={v.get('kalman_finding')} "
        f"blend={v.get('blend_finding')} "
        f"any={v.get('any_finding')}"
    )
    print(f"pairwise {json.dumps(d['pairwise'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
