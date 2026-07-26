#!/usr/bin/env python3
"""Evaluate covariance sizing on two YES tokens from one journal.

Usage:
  uv run python scripts/cov_sizing_eval.py \\
      --journal livecfg/journal/paper.jsonl \\
      --token-a ... --token-b ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.cov_sizing_eval import evaluate_covariance_sizing, write_cov_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--token-a", required=True)
    ap.add_argument("--token-b", required=True)
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--report", default="logs/cov_sizing_eval/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    rep = evaluate_covariance_sizing(
        journal,
        token_a=args.token_a,
        token_b=args.token_b,
        notional=args.notional,
        holdout_frac=args.holdout_frac,
        sample_every=args.sample_every,
    )
    path = write_cov_report(rep, Path(args.report))
    d = rep.as_dict()
    print(f"status=OK journal={journal} report={path}")
    print(
        f"corr_tune={d['corr_tune']} corr_holdout={d['corr_holdout']} "
        f"scale={d['scaling_factor']} var_red={d['variance_reduction']}"
    )
    print(f"bootstrap {json.dumps(d['bootstrap'], sort_keys=True)}")
    print(f"verdict {json.dumps(d['verdict'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
