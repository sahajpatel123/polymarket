#!/usr/bin/env python3
"""Summarize strategy_cycles.jsonl and estimate time to Tier-2 hour gate.

Tier-1 ops view for Agent-1 while paper runtime accumulates.

Usage:
  uv run python scripts/summarize_strategy_cycles.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _f(row: dict[str, Any], *keys: str) -> float | None:
    cur: Any = row
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None or cur == "":
        return None
    try:
        # gate may store "false reason=..." in tier2_allowed
        return float(str(cur).split()[0])
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default="logs/strategy_cycles.jsonl")
    ap.add_argument("--min-hours", type=float, default=24.0)
    args = ap.parse_args()
    path = Path(args.log)
    if not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        return 2

    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        print("status=EMPTY", file=sys.stderr)
        return 2

    first, last = rows[0], rows[-1]
    h0 = _f(first, "gate", "runtime_hours")
    h1 = _f(last, "gate", "runtime_hours")
    q0 = _f(first, "gate", "quotes_for_gate")
    q1 = _f(last, "gate", "quotes_for_gate")
    t0 = datetime.fromisoformat(str(first["ts"]).replace("Z", "+00:00")).timestamp()
    t1 = datetime.fromisoformat(str(last["ts"]).replace("Z", "+00:00")).timestamp()
    wall_h = max(1e-9, (t1 - t0) / 3600.0)

    dh = None if h0 is None or h1 is None else h1 - h0
    dq = None if q0 is None or q1 is None else q1 - q0
    runtime_rate = None if dh is None else dh / wall_h  # paper hours per wall hour (~1 if continuous)
    quote_rate = None if dq is None else dq / wall_h

    remaining_h = None if h1 is None else max(0.0, args.min_hours - h1)
    last_health = (last.get("health") or {}).get("status")
    eta_paused = str(last_health or "").upper() == "STALE"
    eta_wall_h = None
    if (
        not eta_paused
        and remaining_h is not None
        and runtime_rate
        and runtime_rate > 0
    ):
        eta_wall_h = remaining_h / runtime_rate

    rep = {
        "n_cycles": len(rows),
        "first_ts": first.get("ts"),
        "last_ts": last.get("ts"),
        "wall_hours_observed": round(wall_h, 4),
        "runtime_hours_first": h0,
        "runtime_hours_last": h1,
        "quotes_first": q0,
        "quotes_last": q1,
        "runtime_hours_per_wall_hour": None if runtime_rate is None else round(runtime_rate, 4),
        "quotes_per_wall_hour": None if quote_rate is None else round(quote_rate, 2),
        "min_hours_gate": args.min_hours,
        "hours_remaining": None if remaining_h is None else round(remaining_h, 4),
        "eta_wall_hours_to_gate": None if eta_wall_h is None else round(eta_wall_h, 4),
        "eta_paused": eta_paused,
        "last_health": last_health,
        "last_requote_age_s": (last.get("health") or {}).get("last_requote_age_s"),
        "last_tape_frozen": last.get("tape_frozen"),
        "last_spearman": (last.get("rank") or {}).get("spearman"),
        "last_shadow_lifetimes": (last.get("shadow") or {}).get("lifetimes"),
        "last_crossed_frac": (last.get("shadow") or {}).get("crossed_frac"),
        "last_markout_30s": (last.get("shadow") or {}).get("markout_30s"),
        "last_false_trending_frac": (last.get("snapshot") or {}).get(
            "false_trending_frac"
        ),
        "last_false_trending_cancel_share": (last.get("snapshot") or {}).get(
            "false_trending_cancel_share"
        ),
        "last_vol_only_frac": (last.get("snapshot") or {}).get("vol_only_frac"),
        "last_vol_gap": (last.get("snapshot") or {}).get("vol_gap"),
        "last_quiet_vol_max": (last.get("snapshot") or {}).get("quiet_vol_max"),
        "last_trend_vol_min": (last.get("snapshot") or {}).get("trend_vol_min"),
        "last_suggested_vol": (last.get("snapshot") or {}).get("suggested_vol"),
        "last_false_trending_attr_frac": (last.get("snapshot") or {}).get(
            "false_trending_attr_frac"
        ),
        "last_c01_status": (last.get("c01") or {}).get("status"),
        "last_c01_blockers": (last.get("c01") or {}).get("blockers"),
        "last_suppress_2": (last.get("c01") or {}).get("suppress_2"),
        "last_suppress_suggested": (last.get("c01") or {}).get("suppress_suggested"),
        "last_suppress_target": (last.get("c01") or {}).get("suppress_target"),
        "last_outage_alert": (last.get("c01") or {}).get("outage_alert"),
        "last_outage_alert_severe": (last.get("c01") or {}).get("outage_alert_severe"),
        "last_outage_open": (last.get("outage") or {}).get("open"),
        "last_outage_total_h": (last.get("outage") or {}).get("total_h"),
        "last_unused_set": (last.get("unused_knobs") or {}).get("n_set_unused"),
        "last_paper_schema": (last.get("paper_schema") or {}).get("status"),
        "last_connectivity": (last.get("connectivity") or {}).get("status"),
    }
    # Attach compact outage summary when cycles include STALE/DOWN stretches.
    try:
        import importlib.util

        path_mod = Path(__file__).resolve().parent / "outage_window_report.py"
        spec = importlib.util.spec_from_file_location("outage_window_report", path_mod)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            outage = mod.analyze_cycles(rows)
            rep["outage_open"] = outage.get("outage_open")
            rep["outage_total_h"] = outage.get("outage_total_h")
            rep["outage_current_duration_s"] = (outage.get("current") or {}).get(
                "duration_s"
            )
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(rep, indent=2, sort_keys=True))
    print(
        f"status=OK cycles={rep['n_cycles']} runtime_h={h1} "
        f"hours_remaining={rep['hours_remaining']} "
        f"eta_wall_h={rep['eta_wall_hours_to_gate']} eta_paused={eta_paused} "
        f"outage_open={rep.get('outage_open')} outage_total_h={rep.get('outage_total_h')} "
        f"outage_alert={rep['last_outage_alert']} "
        f"outage_alert_severe={rep['last_outage_alert_severe']} "
        f"quotes_per_wall_h={rep['quotes_per_wall_hour']} health={rep['last_health']} "
        f"last_requote_age_s={rep['last_requote_age_s']} "
        f"tape_frozen={rep['last_tape_frozen']} "
        f"connectivity={rep['last_connectivity']} "
        f"crossed_frac={rep['last_crossed_frac']} markout_30s={rep['last_markout_30s']} "
        f"false_trending_frac={rep['last_false_trending_frac']} "
        f"false_trending_cancel_share={rep['last_false_trending_cancel_share']} "
        f"vol_only_frac={rep['last_vol_only_frac']} "
        f"vol_gap={rep['last_vol_gap']} quiet_vol_max={rep['last_quiet_vol_max']} "
        f"trend_vol_min={rep['last_trend_vol_min']} "
        f"suggested_vol={rep['last_suggested_vol']} "
        f"false_trending_attr_frac={rep['last_false_trending_attr_frac']} "
        f"c01={rep['last_c01_status']} c01_blockers={rep['last_c01_blockers']} "
        f"suppress_2={rep['last_suppress_2']} "
        f"suppress_suggested={rep['last_suppress_suggested']} "
        f"suppress_target={rep['last_suppress_target']} "
        f"unused_set={rep['last_unused_set']} "
        f"paper_schema={rep['last_paper_schema']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
