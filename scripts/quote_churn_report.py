#!/usr/bin/env python3
"""Quote lifetime / requote-interval churn report (T2-05 evidence surface).

Usage:
  uv run python scripts/quote_churn_report.py
  uv run python scripts/quote_churn_report.py --log livecfg/logs/metrics-paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.metrics.churn import analyze_quote_churn
from polymaker.metrics.log_discovery import (
    DEFAULT_METRICS_CANDIDATES,
    pick_richest_log,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()
    if args.log:
        path = Path(args.log)
    else:
        path = pick_richest_log(DEFAULT_METRICS_CANDIDATES) or Path(
            DEFAULT_METRICS_CANDIDATES[0]
        )
    if not path.exists():
        print(f"status=NO_LOG path={path}", file=sys.stderr)
        return 2
    rep = analyze_quote_churn(path)
    print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    print(
        f"status=OK lifetimes={rep.n_lifetimes} "
        f"life_p50={rep.lifetime_p50_s:.3f} life_p95={rep.lifetime_p95_s:.3f} "
        f"rq_p50={rep.requote_interval_p50_s:.3f} rq_p95={rep.requote_interval_p95_s:.3f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
