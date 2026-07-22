#!/usr/bin/env python3
"""Shadow adverse-selection from resting quote lifetimes (no fills needed).

Paper mode has 0 fills → classic markouts stay empty. This reports how mid/FV
moved while each quote rested, plus how often mid crossed the resting price.

Usage:
  uv run python scripts/shadow_adverse_selection.py
  uv run python scripts/shadow_adverse_selection.py --log livecfg/logs/metrics-paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.metrics.shadow_as import analyze_shadow_as


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--log",
        default=None,
        help="Metrics JSONL (default: livecfg/logs then logs/)",
    )
    args = ap.parse_args()
    if args.log:
        path = Path(args.log)
    else:
        candidates = [
            Path("livecfg/logs/metrics-paper.jsonl"),
            Path("logs/metrics-paper.jsonl"),
        ]
        path = next((p for p in candidates if p.exists()), candidates[0])

    if not path.exists():
        print(f"status=NO_LOG path={path}", file=sys.stderr)
        return 2

    rep = analyze_shadow_as(path)
    print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    m30 = rep.markout_mean.get("30s", 0.0)
    print(
        f"status=OK lifetimes={rep.n_quote_lifetimes} "
        f"crossed_frac={rep.crossed_frac:.4f} "
        f"mean_edge={rep.mean_edge_at_place:.6f} "
        f"markout_30s={m30:.6f} n30={rep.markout_n.get('30s', 0)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
