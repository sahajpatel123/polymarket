#!/usr/bin/env python3
"""Calibrate flow_z → P(up) with OOS Brier vs tune climatology.

Usage:
  uv run python scripts/flow_calibration.py \\
      --journal livecfg/journal/paper.jsonl --yes-token ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.flow_calibration import calibrate_flow, write_flow_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--flow-halflife-s", type=float, default=90.0)
    ap.add_argument("--report", default="logs/flow_calibration/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    rep = calibrate_flow(
        journal,
        yes_token=args.yes_token,
        no_token=args.no_token,
        horizon_s=args.horizon_s,
        sample_every=args.sample_every,
        holdout_frac=args.holdout_frac,
        flow_halflife_s=args.flow_halflife_s,
    )
    path = write_flow_report(rep, Path(args.report))
    d = rep.as_dict()
    f, v = d["flow"], d["verdict"]
    print(f"status=OK journal={journal} report={path} n={d['n']}")
    print(
        f"flow brier={f.get('brier')} baseline={f.get('brier_baseline')} "
        f"delta={f.get('delta_brier')} p={f.get('paired_p')} finding={v.get('flow_finding')}"
    )
    print(f"verdict {json.dumps(v, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
