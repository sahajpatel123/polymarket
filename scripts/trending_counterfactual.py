#!/usr/bin/env python3
"""Counterfactual TRENDING suppression from paper requote logs (offline).

Given logged ``flowz`` + ``vol_ratio`` (T1-41), estimate how many TRENDING
requotes would flip to QUIET under candidate ``trend_vol_ratio`` /
``trend_flow_z`` thresholds — the C-01 lever — without replaying or
touching live Polymarket.

Usage:
  uv run python scripts/trending_counterfactual.py
  uv run python scripts/trending_counterfactual.py --trend-vol-ratio 8 --trend-flow-z 1.2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from polymaker.metrics.log_discovery import DEFAULT_PAPER_CANDIDATES, pick_richest_log


def analyze_counterfactual(
    path: Path,
    *,
    trend_vol_ratio: float = 8.0,
    trend_flow_z: float = 1.2,
) -> dict[str, Any]:
    vol_thr = float(trend_vol_ratio)
    flow_thr = abs(float(trend_flow_z))
    n_requote = 0
    n_trending = 0
    n_with_vol = 0
    would_suppress = 0
    suppress_cancel = 0
    suppress_place = 0
    keep_cancel = 0
    keep_place = 0
    missing_vol = 0

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("event") or obj.get("msg") or "") != "requote":
                continue
            n_requote += 1
            if str(obj.get("regime") or "") != "TRENDING":
                continue
            n_trending += 1
            n_cancel = int(obj.get("cancel") or 0)
            n_place = int(obj.get("place") or 0)
            try:
                flowz = float(obj.get("flowz"))
            except (TypeError, ValueError):
                flowz = None
            try:
                volr = float(obj["vol_ratio"]) if "vol_ratio" in obj else None
            except (TypeError, ValueError, KeyError):
                volr = None
            if volr is None or flowz is None:
                missing_vol += 1
                continue
            n_with_vol += 1
            # RegimeMachine: TRENDING if |flow_z|>=thr OR vol_ratio>=thr
            still_trend = abs(flowz) >= flow_thr or volr >= vol_thr
            if still_trend:
                keep_cancel += n_cancel
                keep_place += n_place
            else:
                would_suppress += 1
                suppress_cancel += n_cancel
                suppress_place += n_place

    return {
        "path": str(path),
        "candidate_trend_vol_ratio": vol_thr,
        "candidate_trend_flow_z": flow_thr,
        "n_requote": n_requote,
        "n_trending": n_trending,
        "n_trending_with_vol": n_with_vol,
        "n_trending_missing_vol": missing_vol,
        "would_suppress_n": would_suppress,
        "would_suppress_frac": (
            round(would_suppress / n_with_vol, 6) if n_with_vol else None
        ),
        "suppress_cancel_sum": suppress_cancel,
        "suppress_place_sum": suppress_place,
        "keep_cancel_sum": keep_cancel,
        "keep_place_sum": keep_place,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    ap.add_argument("--trend-vol-ratio", type=float, default=8.0)
    ap.add_argument("--trend-flow-z", type=float, default=1.2)
    args = ap.parse_args()
    path = Path(args.log) if args.log else pick_richest_log(DEFAULT_PAPER_CANDIDATES)
    if path is None or not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        return 2
    rep = analyze_counterfactual(
        path,
        trend_vol_ratio=args.trend_vol_ratio,
        trend_flow_z=args.trend_flow_z,
    )
    print(json.dumps(rep, indent=2, sort_keys=True))
    print(
        f"status=OK trending={rep['n_trending']} with_vol={rep['n_trending_with_vol']} "
        f"suppress_n={rep['would_suppress_n']} suppress_frac={rep['would_suppress_frac']} "
        f"suppress_cancel={rep['suppress_cancel_sum']} "
        f"vol={rep['candidate_trend_vol_ratio']} flowz={rep['candidate_trend_flow_z']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
