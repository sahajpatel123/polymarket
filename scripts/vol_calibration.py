#!/usr/bin/env python3
"""Calibrate GARCH(1,1) vs EWMA vol forecasts (OOS MSE + significance).

Usage:
  uv run python scripts/vol_calibration.py \\
      --journal livecfg/journal/paper.jsonl \\
      --yes-token ... --no-token ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.vol_calibration import calibrate_vol_models, write_vol_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--report", default="logs/vol_calibration/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    rep = calibrate_vol_models(
        journal,
        yes_token=args.yes_token,
        no_token=args.no_token,
        horizon_s=args.horizon_s,
        sample_every=args.sample_every,
        holdout_frac=args.holdout_frac,
    )
    path = write_vol_report(rep, Path(args.report))
    d = rep.as_dict()
    v = d["verdict"]
    print(f"status=OK journal={journal} report={path} n={d['n']}")
    print(f"garch_mse={d['garch'].get('mse')} ewma_mse={d['ewma'].get('mse')}")
    print(f"verdict garch_finding={v.get('garch_finding')} sig={json.dumps(v.get('significance'), sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
