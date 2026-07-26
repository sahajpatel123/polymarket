#!/usr/bin/env python3
"""Calibrate virtual markout toxicity → P(big move).

Usage:
  uv run python scripts/toxicity_calibration.py \\
      --journal livecfg/journal/paper.jsonl --yes-token ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.toxicity_calibration import calibrate_toxicity, write_toxicity_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--report", default="logs/toxicity_calibration/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    rep = calibrate_toxicity(
        journal,
        yes_token=args.yes_token,
        no_token=args.no_token,
        horizon_s=args.horizon_s,
        sample_every=args.sample_every,
        holdout_frac=args.holdout_frac,
    )
    path = write_toxicity_report(rep, Path(args.report))
    d = rep.as_dict()
    t, v = d["toxicity"], d["verdict"]
    print(f"status=OK journal={journal} report={path} n={d['n']}")
    print(
        f"tox brier={t.get('brier')} baseline={t.get('brier_baseline')} "
        f"delta={t.get('delta_brier')} p={t.get('paired_p')} finding={v.get('toxicity_finding')}"
    )
    print(f"verdict {json.dumps(v, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
