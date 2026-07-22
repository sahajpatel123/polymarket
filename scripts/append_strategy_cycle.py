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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="logs/strategy_cycles.jsonl")
    args = ap.parse_args()

    py = sys.executable
    codes = {}
    statuses = {}
    for name, cmd in (
        ("snapshot", [py, "scripts/strategy_snapshot.py"]),
        ("rank", [py, "scripts/rank_vs_realized.py"]),
        ("gate", [py, "scripts/paper_data_gate.py"]),
        ("health", [py, "scripts/paper_health.py"]),
        ("shadow", [py, "scripts/shadow_adverse_selection.py"]),
    ):
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
        "gate": statuses.get("gate_full", statuses.get("gate", {})),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps(row, indent=2, sort_keys=True))
    g = row["gate"]
    h = row["health"]
    sh = row["shadow"]
    print(
        f"status=OK appended={out} runtime_h={g.get('runtime_hours')} "
        f"quotes={g.get('quotes_for_gate')} tier2={g.get('tier2_allowed')} "
        f"spearman={row['rank'].get('spearman')} health={h.get('status')} "
        f"shadow_lifetimes={sh.get('lifetimes')} "
        f"crossed_frac={sh.get('crossed_frac')} "
        f"markout_30s={sh.get('markout_30s')}",
        file=sys.stderr,
    )
    # health returncode 1 = stale; still append evidence but surface non-zero
    return 0 if codes.get("gate", 1) == 0 and codes.get("snapshot", 1) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
