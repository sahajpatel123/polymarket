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


def analyze_paper_log(path: Path) -> dict[str, Any]:
    regimes = Counter()
    regimes_by_cid: dict[str, Counter] = defaultdict(Counter)
    transitions: Counter = Counter()
    last_regime: dict[str, str] = {}
    cancel_sum = 0
    place_sum = 0
    n_requote = 0
    trending_flowz: list[float] = []
    quiet_flowz: list[float] = []
    n_bad = 0
    n_lines = 0

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
            cancel_sum += int(obj.get("cancel") or 0)
            place_sum += int(obj.get("place") or 0)
            try:
                flowz = float(obj.get("flowz"))
            except (TypeError, ValueError):
                flowz = None
            if flowz is not None:
                if regime == "TRENDING":
                    trending_flowz.append(flowz)
                elif regime == "QUIET":
                    quiet_flowz.append(flowz)
            prev = last_regime.get(cid)
            if prev is not None and prev != regime:
                transitions[f"{prev}->{regime}"] += 1
            last_regime[cid] = regime

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 6) if xs else None

    churn = round(cancel_sum / place_sum, 6) if place_sum else None
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
        "trending_frac": round(regimes.get("TRENDING", 0) / n_requote, 6) if n_requote else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    args = ap.parse_args()
    path = Path(args.log) if args.log else pick_richest_log(DEFAULT_PAPER_CANDIDATES)
    if path is None or not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        print(json.dumps({"status": "NO_LOG"}, indent=2))
        return 0
    rep = analyze_paper_log(path)
    print(json.dumps(rep, indent=2, sort_keys=True))
    print(
        f"status=OK requotes={rep['n_requote']} trending_frac={rep['trending_frac']} "
        f"cancel_per_place={rep['cancel_per_place']} "
        f"transitions={sum(rep['regime_transitions'].values())}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
