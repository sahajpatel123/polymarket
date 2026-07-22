#!/usr/bin/env python3
"""Build a local HTML metrics dashboard from the T1-01 metrics log (T1-08).

Usage:
  uv run python scripts/metrics_dashboard.py
  uv run python scripts/metrics_dashboard.py --log logs/metrics-paper.jsonl --out logs/dashboard.html
  open logs/dashboard.html   # glance health without reading raw JSONL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.metrics.dashboard import write_dashboard


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default="logs/metrics-paper.jsonl")
    ap.add_argument("--out", default="logs/dashboard.html")
    args = ap.parse_args()
    log_path = Path(args.log)
    out = Path(args.out)
    report = write_dashboard(log_path, out)
    summary = {
        "out": str(out.resolve()),
        "health_quotes": report.n_quote,
        "health_fills": report.n_fill,
        "realized_spread_usdc": report.realized_spread_usdc,
        "inventory_drift_abs_peak": report.inventory_drift_abs_peak,
        "markets": sorted(report.markets),
        "exists_log": log_path.exists(),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"status=OK wrote={out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
