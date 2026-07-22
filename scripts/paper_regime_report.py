#!/usr/bin/env python3
"""Summarize regime mix and quote churn from a paper.jsonl structlog file.

Tier-1 evidence tool for future T2-04/T2-05 work. Reads only; no strategy edits.

Usage:
  uv run python scripts/paper_regime_report.py
  uv run python scripts/paper_regime_report.py --log livecfg/logs/paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


from polymaker.metrics.log_discovery import DEFAULT_PAPER_CANDIDATES, pick_richest_log


def analyze_paper_log(
    path: Path,
    *,
    trend_flow_z: float = 1.2,
    trend_vol_ratio: float = 2.0,
) -> dict[str, Any]:
    """Summarize requote regimes.

    false_trending_frac: share of TRENDING requotes with |flow_z| < trend_flow_z
    — the vol-ratio-only trips that C-01 targets (T1-39).

    When requotes include ``vol_ratio`` (T1-41), also attribute TRENDING as
    flow_only / vol_only / both / missing_vol (legacy lines without the field).
    """
    regimes = Counter()
    regimes_by_cid: dict[str, Counter] = defaultdict(Counter)
    transitions: Counter = Counter()
    last_regime: dict[str, str] = {}
    cancel_sum = 0
    place_sum = 0
    n_requote = 0
    trending_flowz: list[float] = []
    quiet_flowz: list[float] = []
    trending_vol: list[float] = []
    quiet_vol: list[float] = []
    false_trending = 0
    false_cancel = 0
    false_place = 0
    false_trending_attr = 0
    trend_cancel = 0
    trend_place = 0
    path_counts: Counter[str] = Counter()
    n_bad = 0
    n_lines = 0
    flow_thresh = abs(float(trend_flow_z))
    vol_thresh = float(trend_vol_ratio)

    with path.open() as fh:
        for line in fh:
            n_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                continue
            if not isinstance(obj, dict):
                n_bad += 1
                continue
            if str(obj.get("event") or obj.get("msg") or "") != "requote":
                continue
            n_requote += 1
            regime = str(obj.get("regime") or "UNKNOWN")
            cid = str(obj.get("condition_id") or obj.get("cid") or "unknown")
            regimes[regime] += 1
            regimes_by_cid[cid][regime] += 1
            n_cancel = int(obj.get("cancel") or 0)
            n_place = int(obj.get("place") or 0)
            cancel_sum += n_cancel
            place_sum += n_place
            try:
                flowz = float(obj.get("flowz"))
            except (TypeError, ValueError):
                flowz = None
            vol_raw = obj.get("vol_ratio")
            try:
                volr = float(vol_raw) if vol_raw is not None else None
            except (TypeError, ValueError):
                volr = None
            if regime == "TRENDING":
                trend_cancel += n_cancel
                trend_place += n_place
                if flowz is not None and volr is not None:
                    flow_hit = abs(flowz) >= flow_thresh
                    vol_hit = volr >= vol_thresh
                    if flow_hit and vol_hit:
                        path_counts["both"] += 1
                    elif flow_hit:
                        path_counts["flow_only"] += 1
                    elif vol_hit:
                        path_counts["vol_only"] += 1
                    else:
                        # Below both thresholds yet labeled TRENDING — stale/bug.
                        path_counts["neither"] += 1
                    trending_vol.append(volr)
                    if abs(flowz) < flow_thresh:
                        false_trending_attr += 1
                elif flowz is not None:
                    path_counts["missing_vol"] += 1
                else:
                    path_counts["missing_flowz"] += 1
            if flowz is not None:
                if regime == "TRENDING":
                    trending_flowz.append(flowz)
                    if abs(flowz) < flow_thresh:
                        false_trending += 1
                        false_cancel += n_cancel
                        false_place += n_place
                elif regime == "QUIET":
                    quiet_flowz.append(flowz)
            if volr is not None and regime == "QUIET":
                quiet_vol.append(volr)
            prev = last_regime.get(cid)
            if prev is not None and prev != regime:
                transitions[f"{prev}->{regime}"] += 1
            last_regime[cid] = regime

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 6) if xs else None

    def _pct(xs: list[float], p: float) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        if len(s) == 1:
            return round(s[0], 6)
        idx = (p / 100.0) * (len(s) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(s) - 1)
        frac = idx - lo
        return round(s[lo] * (1.0 - frac) + s[hi] * frac, 6)

    def _vol_summary(xs: list[float]) -> dict[str, float | int | None]:
        return {
            "n": len(xs),
            "min": round(min(xs), 6) if xs else None,
            "p50": _pct(xs, 50),
            "p90": _pct(xs, 90),
            "max": round(max(xs), 6) if xs else None,
            "mean": _mean(xs),
        }

    churn = round(cancel_sum / place_sum, 6) if place_sum else None
    n_trend = regimes.get("TRENDING", 0)
    n_attributed = (
        path_counts["both"]
        + path_counts["flow_only"]
        + path_counts["vol_only"]
        + path_counts["neither"]
    )
    quiet_vol_sum = _vol_summary(quiet_vol)
    trend_vol_sum = _vol_summary(trending_vol)
    # Separation: how far QUIET max sits below TRENDING min (C-01 threshold room).
    sep = None
    if quiet_vol_sum["max"] is not None and trend_vol_sum["min"] is not None:
        sep = round(float(trend_vol_sum["min"]) - float(quiet_vol_sum["max"]), 6)
    # Informational C-01 floor: sit 0.5 above observed QUIET max (not a merge trigger).
    suggested = None
    if quiet_vol_sum["max"] is not None:
        suggested = round(float(quiet_vol_sum["max"]) + 0.5, 3)
    return {
        "path": str(path),
        "n_lines": n_lines,
        "n_bad": n_bad,
        "n_requote": n_requote,
        "regimes": dict(regimes),
        "regimes_by_market": {k: dict(v) for k, v in sorted(regimes_by_cid.items())},
        "regime_transitions": dict(transitions),
        "cancel_sum": cancel_sum,
        "place_sum": place_sum,
        "cancel_per_place": churn,
        "trending_flowz_n": len(trending_flowz),
        "trending_flowz_mean": _mean(trending_flowz),
        "quiet_flowz_mean": _mean(quiet_flowz),
        "trending_vol_ratio_mean": _mean(trending_vol),
        "quiet_vol_ratio": quiet_vol_sum,
        "trending_vol_ratio": trend_vol_sum,
        "vol_ratio_quiet_trend_gap": sep,
        "suggested_trend_vol_ratio": suggested,
        "trending_frac": round(n_trend / n_requote, 6) if n_requote else 0.0,
        "trend_flow_z_threshold": flow_thresh,
        "trend_vol_ratio_threshold": vol_thresh,
        "false_trending_n": false_trending,
        "false_trending_frac": round(false_trending / n_trend, 6) if n_trend else 0.0,
        # Post-T1-41 only: ignore missing_vol legacy TRENDING rows (T1-57).
        "false_trending_attributed_n": false_trending_attr,
        "false_trending_attributed_frac": (
            round(false_trending_attr / n_attributed, 6) if n_attributed else None
        ),
        # Share of all cancels/places that happened on false-TRENDING requotes —
        # upper-bound C-01 churn impact if those trips were suppressed (T1-40).
        "false_trending_cancel_sum": false_cancel,
        "false_trending_place_sum": false_place,
        "trending_cancel_sum": trend_cancel,
        "trending_place_sum": trend_place,
        "false_trending_cancel_share": (
            round(false_cancel / cancel_sum, 6) if cancel_sum else 0.0
        ),
        "false_trending_place_share": (
            round(false_place / place_sum, 6) if place_sum else 0.0
        ),
        # Dual-path attribution when vol_ratio is logged (T1-41).
        "trending_path": dict(path_counts),
        "trending_path_attributed_n": n_attributed,
        "trending_vol_only_frac": (
            round(path_counts["vol_only"] / n_attributed, 6) if n_attributed else None
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    ap.add_argument(
        "--trend-flow-z",
        type=float,
        default=1.2,
        help=" |flow_z| below this on TRENDING counts as false_trending (default 1.2)",
    )
    ap.add_argument(
        "--trend-vol-ratio",
        type=float,
        default=2.0,
        help=" vol_ratio threshold for dual-path attribution (default 2.0)",
    )
    args = ap.parse_args()
    path = Path(args.log) if args.log else pick_richest_log(DEFAULT_PAPER_CANDIDATES)
    if path is None or not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        print(json.dumps({"status": "NO_LOG"}, indent=2))
        return 0
    rep = analyze_paper_log(
        path,
        trend_flow_z=args.trend_flow_z,
        trend_vol_ratio=args.trend_vol_ratio,
    )
    print(json.dumps(rep, indent=2, sort_keys=True))
    print(
        f"status=OK requotes={rep['n_requote']} trending_frac={rep['trending_frac']} "
        f"false_trending_frac={rep['false_trending_frac']} "
        f"false_trending_attr_frac={rep['false_trending_attributed_frac']} "
        f"false_trending_cancel_share={rep['false_trending_cancel_share']} "
        f"false_trending_place_share={rep['false_trending_place_share']} "
        f"vol_only_frac={rep['trending_vol_only_frac']} "
        f"quiet_vol_max={(rep.get('quiet_vol_ratio') or {}).get('max')} "
        f"quiet_vol_p90={(rep.get('quiet_vol_ratio') or {}).get('p90')} "
        f"trend_vol_min={(rep.get('trending_vol_ratio') or {}).get('min')} "
        f"trend_vol_p50={(rep.get('trending_vol_ratio') or {}).get('p50')} "
        f"vol_gap={rep.get('vol_ratio_quiet_trend_gap')} "
        f"suggested_vol={rep.get('suggested_trend_vol_ratio')} "
        f"path={rep['trending_path']} "
        f"cancel_per_place={rep['cancel_per_place']} "
        f"transitions={sum(rep['regime_transitions'].values())}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
