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

from polymaker.replay.fv_calibration import calibrate_fair_value, write_fv_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--micro-levels", type=int, default=3)
    ap.add_argument("--report", default="logs/fv_calibration/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

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
