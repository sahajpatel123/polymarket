#!/usr/bin/env python3
"""Counterfactual TRENDING suppression from paper requote logs (offline).

Given logged ``flowz`` + ``vol_ratio`` (T1-41), estimate how many TRENDING
requotes would flip to QUIET under candidate ``trend_vol_ratio`` /
``trend_flow_z`` thresholds — the C-01 lever — without replaying or
touching live Polymarket.

Usage:
  uv run python scripts/trending_counterfactual.py
  uv run python scripts/trending_counterfactual.py --trend-vol-ratio 8 --trend-flow-z 1.2
  uv run python scripts/trending_counterfactual.py --sweep-vol 3,5,8 --by-market
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from polymaker.metrics.log_discovery import DEFAULT_PAPER_CANDIDATES, pick_richest_log


def _load_trending_rows(path: Path) -> tuple[int, list[dict[str, Any]]]:
    n_requote = 0
    rows: list[dict[str, Any]] = []
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
            try:
                flowz = float(obj.get("flowz"))
            except (TypeError, ValueError):
                flowz = None
            try:
                volr = float(obj["vol_ratio"]) if "vol_ratio" in obj else None
            except (TypeError, ValueError, KeyError):
                volr = None
            rows.append({
                "condition_id": str(obj.get("condition_id") or obj.get("cid") or "unknown"),
                "flowz": flowz,
                "vol_ratio": volr,
                "cancel": int(obj.get("cancel") or 0),
                "place": int(obj.get("place") or 0),
            })
    return n_requote, rows


def _score_rows(
    rows: list[dict[str, Any]],
    *,
    trend_vol_ratio: float,
    trend_flow_z: float,
) -> dict[str, Any]:
    vol_thr = float(trend_vol_ratio)
    flow_thr = abs(float(trend_flow_z))
    n_trending = len(rows)
    n_with_vol = 0
    would_suppress = 0
    suppress_cancel = 0
    suppress_place = 0
    keep_cancel = 0
    keep_place = 0
    missing_vol = 0

    for row in rows:
        flowz = row["flowz"]
        volr = row["vol_ratio"]
        n_cancel = row["cancel"]
        n_place = row["place"]
        if volr is None or flowz is None:
            missing_vol += 1
            continue
        n_with_vol += 1
        still_trend = abs(flowz) >= flow_thr or volr >= vol_thr
        if still_trend:
            keep_cancel += n_cancel
            keep_place += n_place
        else:
            would_suppress += 1
            suppress_cancel += n_cancel
            suppress_place += n_place

    return {
        "candidate_trend_vol_ratio": vol_thr,
        "candidate_trend_flow_z": flow_thr,
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


def analyze_counterfactual(
    path: Path,
    *,
    trend_vol_ratio: float = 8.0,
    trend_flow_z: float = 1.2,
    condition_id: str | None = None,
) -> dict[str, Any]:
    n_requote, rows = _load_trending_rows(path)
    if condition_id:
        rows = [r for r in rows if r["condition_id"] == condition_id]
    scored = _score_rows(
        rows, trend_vol_ratio=trend_vol_ratio, trend_flow_z=trend_flow_z
    )
    return {"path": str(path), "n_requote": n_requote, **scored}


def sweep_counterfactual(
    path: Path,
    *,
    vol_values: list[float],
    trend_flow_z: float = 1.2,
    by_market: bool = False,
) -> dict[str, Any]:
    n_requote, rows = _load_trending_rows(path)
    flow_thr = abs(float(trend_flow_z))
    markets = sorted({r["condition_id"] for r in rows}) if by_market else [None]
    out_markets: list[dict[str, Any]] = []
    for cid in markets:
        subset = rows if cid is None else [r for r in rows if r["condition_id"] == cid]
        sweep_rows = [
            _score_rows(subset, trend_vol_ratio=v, trend_flow_z=flow_thr)
            for v in vol_values
        ]
        out_markets.append({
            "condition_id": cid or "ALL",
            "n_trending": len(subset),
            "sweep": sweep_rows,
        })
    return {
        "path": str(path),
        "n_requote": n_requote,
        "candidate_trend_flow_z": flow_thr,
        "vol_values": vol_values,
        "by_market": by_market,
        "markets": out_markets,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    ap.add_argument("--trend-vol-ratio", type=float, default=8.0)
    ap.add_argument("--trend-flow-z", type=float, default=1.2)
    ap.add_argument(
        "--sweep-vol",
        default=None,
        help="Comma-separated trend_vol_ratio values to sweep (overrides single value)",
    )
    ap.add_argument("--by-market", action="store_true")
    ap.add_argument("--condition-id", default=None)
    args = ap.parse_args()
    path = Path(args.log) if args.log else pick_richest_log(DEFAULT_PAPER_CANDIDATES)
    if path is None or not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        return 2

    if args.sweep_vol:
        vals = [float(x.strip()) for x in args.sweep_vol.split(",") if x.strip()]
        rep = sweep_counterfactual(
            path,
            vol_values=vals,
            trend_flow_z=args.trend_flow_z,
            by_market=args.by_market,
        )
        print(json.dumps(rep, indent=2, sort_keys=True))
        # Compact status: per market suppress_frac at each vol
        parts = []
        for m in rep["markets"]:
            fracs = ",".join(
                f"{r['candidate_trend_vol_ratio']}={r['would_suppress_frac']}"
                for r in m["sweep"]
            )
            parts.append(f"{m['condition_id'][:10]}:{fracs}")
        print(
            f"status=OK mode=sweep flowz={rep['candidate_trend_flow_z']} "
            f"markets={len(rep['markets'])} " + " ".join(parts),
            file=sys.stderr,
        )
        return 0

    rep = analyze_counterfactual(
        path,
        trend_vol_ratio=args.trend_vol_ratio,
        trend_flow_z=args.trend_flow_z,
        condition_id=args.condition_id,
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
