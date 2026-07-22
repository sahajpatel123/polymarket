#!/usr/bin/env python3
"""Print paper/live metrics from the structured metrics JSONL log.

Done-criteria evidence for backlog item T1-01 (paper-trading metrics logger).

Usage:
  uv run python scripts/paper_metrics.py
  uv run python scripts/paper_metrics.py --log logs/metrics-paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.metrics.analyze import analyze
from polymaker.metrics.log_discovery import (
    DEFAULT_METRICS_CANDIDATES,
    pick_richest_log,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--log",
        default=None,
        help="Metrics JSONL (default: richest among "
             "livecfg/logs/metrics-paper.jsonl, logs/metrics-paper.jsonl)",
    )
    args = ap.parse_args()
    if args.log:
        path = Path(args.log)
    else:
        path = pick_richest_log(DEFAULT_METRICS_CANDIDATES) or Path(
            DEFAULT_METRICS_CANDIDATES[0]
        )
    rep = analyze(path)
    print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    if not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        return 0
    if rep.n_bad and rep.n_quote + rep.n_fill + rep.n_cancel == 0:
        print("status=CORRUPT", file=sys.stderr)
        return 2
    print(
        f"status=OK quotes={rep.n_quote} cancels={rep.n_cancel} fills={rep.n_fill} "
        f"marks={rep.n_mark} realized_spread_usdc={rep.realized_spread_usdc:.6f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
