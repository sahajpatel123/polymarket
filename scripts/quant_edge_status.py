#!/usr/bin/env python3
"""Print Quantitative Edge inventory + evidence gate status.

Usage:
  uv run python scripts/quant_edge_status.py
"""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.quant_edge import TECHNIQUE_INVENTORY


def main() -> int:
    print("status=OK source=TECHNIQUE_INVENTORY")
    evidence_counts = {"yes": 0, "mixed": 0, "partial": 0, "no": 0, "other": 0}
    for t in TECHNIQUE_INVENTORY:
        ev = str(t.get("evidence") or "other")
        if ev not in evidence_counts:
            evidence_counts["other"] += 1
        else:
            evidence_counts[ev] += 1
        print(
            f"technique id={t['id']} module={t.get('module')} "
            f"wired={t.get('wired')} evidence={t.get('evidence')}"
        )
    print(
        "summary "
        f"n={len(TECHNIQUE_INVENTORY)} "
        f"evidence_yes={evidence_counts['yes']} "
        f"mixed={evidence_counts['mixed']} "
        f"partial={evidence_counts['partial']} "
        f"no={evidence_counts['no']}"
    )
    print(
        "gate_note "
        "Tier-2 quote wiring requires finding=true on fresh paper with "
        "calibration+OOS+CI; mixed/single-market results do not promote"
    )
    print(
        "as_path_note "
        "join_best_bid frozen: tape sells are at-touch only (T1-152); "
        "conservative equal-price skip blocks fills (T1-151); "
        "finding requires n_fill_candidate>0 (T1-153)"
    )

    # Latest report paths if present
    roots = [
        Path("logs/fv_calibration"),
        Path("logs/signal_calibration"),
        Path("logs/toxicity_calibration"),
        Path("logs/kelly_fraction_sweep"),
        Path("logs/quant_edge_eval"),
        Path("logs/through_price_tape"),
        Path("logs/reward_path_compare"),
        Path("logs/queue_ahead_sweep"),
    ]
    latest: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        files = sorted(root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:2]:
            latest.append(str(f))
    if latest:
        print("recent_reports " + json.dumps(latest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
