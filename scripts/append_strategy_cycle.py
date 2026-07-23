#!/usr/bin/env python3
"""Append one strategy-loop cycle evidence line to a JSONL history file.

Tier-1 longitudinal log so Agent-1 ticks leave a durable trail while waiting
on the 24h Tier-2 gate. Does not change strategy math.

Usage:
  uv run python scripts/append_strategy_cycle.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _run_capture(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_status_line(stderr: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stderr.splitlines():
        if not line.startswith("status="):
            continue
        # tokenized key=value pairs after status=
        for part in line.split():
            if "=" in part:
                k, _, v = part.partition("=")
                out[k] = v
    return out


def _load_outage_status(path: Path) -> dict:
    """Load compact outage/gate snapshot for the cycle trail (T1-82)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="logs/strategy_cycles.jsonl")
    ap.add_argument(
        "--outage-status",
        default="logs/outage_status.json",
        help="Compact outage/gate JSON to embed in the cycle row",
    )
    ap.add_argument(
        "--skip-connectivity",
        action="store_true",
        help="Skip Polymarket REST/WS probe (faster during known outages)",
    )
    ap.add_argument(
        "--with-counterfactual",
        action="store_true",
        help="Record offline C-01 suppress_frac@vol=8 (no network)",
    )
    args = ap.parse_args()

    py = sys.executable
    codes = {}
    statuses = {}
    cmds: list[tuple[str, list[str]]] = [
        ("snapshot", [py, "scripts/strategy_snapshot.py"]),
        ("rank", [py, "scripts/rank_vs_realized.py"]),
        ("gate", [py, "scripts/paper_data_gate.py"]),
        ("health", [py, "scripts/paper_health.py"]),
        ("shadow", [py, "scripts/shadow_adverse_selection.py"]),
        ("churn", [py, "scripts/quote_churn_report.py"]),
        ("schema", [py, "scripts/verify_metrics_schema.py", "--tail", "50"]),
        ("paper_schema", [py, "scripts/verify_paper_schema.py", "--tail", "50"]),
        ("c01", [py, "scripts/c01_promotion_checklist.py"]),
        (
            "outage",
            [
                py,
                "scripts/outage_window_report.py",
                "--status-out",
                args.outage_status,
            ],
        ),
        ("unused_knobs", [py, "scripts/unused_knob_toml_scan.py"]),
    ]
    if not args.skip_connectivity:
        cmds.append(
            ("connectivity", [py, "scripts/polymarket_connectivity.py", "--timeout-s", "5"])
        )
    if args.with_counterfactual:
        cmds.append(
            (
                "counterfactual",
                [
                    py,
                    "scripts/trending_counterfactual.py",
                    "--sweep-vol",
                    "8",
                    "--by-market",
                ],
            )
        )
    for name, cmd in cmds:
        code, stdout, stderr = _run_capture(cmd)
        codes[name] = code
        # gate prints status on stdout; others on stderr
        statuses[name] = _parse_status_line(stderr) or _parse_status_line(stdout)
        if name == "gate":
            # keep full gate kv lines
            gate_kv = {}
            for line in stdout.splitlines():
                if "=" in line and not line.startswith("{"):
                    k, _, v = line.partition("=")
                    if k and " " not in k:
                        gate_kv[k] = v
            statuses["gate_full"] = gate_kv

    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "returncodes": codes,
        "snapshot": statuses.get("snapshot", {}),
        "rank": statuses.get("rank", {}),
        "health": statuses.get("health", {}),
        "shadow": statuses.get("shadow", {}),
        "churn": statuses.get("churn", {}),
        "schema": statuses.get("schema", {}),
        "paper_schema": statuses.get("paper_schema", {}),
        "c01": statuses.get("c01", {}),
        "outage": statuses.get("outage", {}),
        "unused_knobs": statuses.get("unused_knobs", {}),
        "connectivity": statuses.get(
            "connectivity",
            {"status": "SKIPPED"} if args.skip_connectivity else {},
        ),
        "counterfactual": statuses.get("counterfactual", {}),
        "gate": statuses.get("gate_full", statuses.get("gate", {})),
    }
    # Shadow/churn/markout on a STALE collector are frozen-tape snapshots — not
    # live adverse-selection signal (T1-65).
    health_status = str((row.get("health") or {}).get("status") or "")
    row["tape_frozen"] = health_status.upper() == "STALE"
    ost = _load_outage_status(Path(args.outage_status))
    # Prefer this-cycle gate fields when compact status is missing them.
    g = row.get("gate") or {}
    if ost is not None:
        if "tier2_allowed" not in ost and "tier2_allowed" in g:
            ost["tier2_allowed"] = str(g.get("tier2_allowed")).lower() == "true"
        if "gate_reason" not in ost and "reason" in g:
            ost["gate_reason"] = g.get("reason")
        if "runtime_basis" not in ost and "runtime_basis" in g:
            ost["runtime_basis"] = g.get("runtime_basis")
    row["outage_status"] = ost
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps(row, indent=2, sort_keys=True))
    g = row["gate"]
    h = row["health"]
    sh = row["shadow"]
    ch = row["churn"]
    sch = row["schema"]
    psch = row["paper_schema"]
    conn = row["connectivity"]
    snap = row["snapshot"]
    cf = row.get("counterfactual") or {}
    c01 = row.get("c01") or {}
    outage = row.get("outage") or {}
    unused = row.get("unused_knobs") or {}
    ost = row.get("outage_status") or {}
    tape_frozen = bool(row.get("tape_frozen"))
    print(
        f"status=OK appended={out} runtime_h={g.get('runtime_hours')} "
        f"quotes={g.get('quotes_for_gate')} tier2={g.get('tier2_allowed')} "
        f"spearman={row['rank'].get('spearman')} health={h.get('status')} "
        f"last_requote_age_s={h.get('last_requote_age_s')} "
        f"tape_frozen={tape_frozen} "
        f"connectivity={conn.get('status')} "
        f"shadow_lifetimes={sh.get('lifetimes')} "
        f"crossed_frac={sh.get('crossed_frac')} "
        f"markout_30s={sh.get('markout_30s')} "
        f"life_p50={ch.get('life_p50')} rq_p50={ch.get('rq_p50')} "
        f"schema={sch.get('status')} paper_schema={psch.get('status')} "
        f"false_trending_frac={snap.get('false_trending_frac')} "
        f"false_trending_cancel_share={snap.get('false_trending_cancel_share')} "
        f"vol_only_frac={snap.get('vol_only_frac')} "
        f"vol_gap={snap.get('vol_gap')} quiet_vol_max={snap.get('quiet_vol_max')} "
        f"trend_vol_min={snap.get('trend_vol_min')} "
        f"suggested_vol={snap.get('suggested_vol')} "
        f"false_trending_attr_frac={snap.get('false_trending_attr_frac')} "
        f"c01={c01.get('status')} c01_blockers={c01.get('blockers')} "
        f"suppress_2={c01.get('suppress_2')} "
        f"suppress_suggested={c01.get('suppress_suggested')} "
        f"suppress_target={c01.get('suppress_target')} "
        f"outage_open={outage.get('open')} outage_total_h={outage.get('total_h')} "
        f"outage_alert={c01.get('outage_alert')} "
        f"outage_alert_severe={c01.get('outage_alert_severe')} "
        f"outage_alert_prolonged={c01.get('outage_alert_prolonged')} "
        f"outage_alert_critical={c01.get('outage_alert_critical')} "
        f"hours_to_tier2_gate={ost.get('hours_to_tier2_gate')} "
        f"unused_set={unused.get('n_set_unused')} "
        f"counterfactual={cf.get('status') or cf.get('mode') or '-'}",
        file=sys.stderr,
    )

    # health returncode 1 = stale; still append evidence but surface non-zero
    return 0 if codes.get("gate", 1) == 0 and codes.get("snapshot", 1) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
