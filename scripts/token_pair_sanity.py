#!/usr/bin/env python3
"""Validate YES/NO token pairing (mids should sum ≈ 1).

Usage:
  uv run python scripts/token_pair_sanity.py \\
      --journal livecfg/journal/paper.jsonl \\
      --yes-token … --no-token …
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from polymaker.replay.token_pair_sanity import (
    assess_token_pair,
    write_token_pair_sanity,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", required=True)
    ap.add_argument("--tol", type=float, default=0.02)
    ap.add_argument("--report", default="logs/token_pair_sanity/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    report = assess_token_pair(
        journal, args.yes_token, args.no_token, tol=args.tol
    )
    path = write_token_pair_sanity(report, Path(args.report))
    d = report.as_dict()
    print(
        f"status=OK pair_ok={d['pair_ok']} reason={d['reason']} "
        f"mean_sum={d['mean_sum']} n={d['n_samples']} report={path}"
    )
    return 0 if report.pair_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
