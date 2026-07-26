#!/usr/bin/env python3
"""Calibrate OFI / VPIN / Kyle signals with proper scoring (OOS).

Usage:
  uv run python scripts/signal_calibration.py \\
      --journal livecfg/journal/paper.jsonl \\
      --yes-token ... --no-token ... \\
      --holdout-frac 0.3 --horizon-s 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.signal_calibration import calibrate_signals, write_signal_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--horizon-s", type=float, default=30.0)
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--report", default="logs/signal_calibration/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    rep = calibrate_signals(
        journal,
        yes_token=args.yes_token,
        no_token=args.no_token,
        horizon_s=args.horizon_s,
        sample_every=args.sample_every,
        holdout_frac=args.holdout_frac,
    )
    path = write_signal_report(rep, Path(args.report))
    d = rep.as_dict()
    ofi, vpin, kyle, v = d["ofi"], d["vpin"], d["kyle"], d["verdict"]
    print(f"status=OK journal={journal} report={path}")
    print(
        f"ofi n={ofi.get('n')} brier={ofi.get('brier')} baseline={ofi.get('brier_baseline')} "
        f"delta={ofi.get('delta_brier')} p={ofi.get('paired_p')} finding={v.get('ofi_finding')}"
    )
    print(
        f"vpin n={vpin.get('n')} brier={vpin.get('brier')} baseline={vpin.get('brier_baseline')} "
        f"delta={vpin.get('delta_brier')} p={vpin.get('paired_p')} finding={v.get('vpin_finding')}"
    )
    print(f"kyle {json.dumps(kyle, sort_keys=True)}")
    print(f"verdict any_finding={v.get('any_finding')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
