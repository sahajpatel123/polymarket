#!/usr/bin/env python3
"""C-01 promotion readiness checklist (Tier-1, offline-friendly).

Aggregates gate / counterfactual / outage / last evidence-pack OOS flags so
Agent-1 can see what still blocks a Tier-2 ``trend_vol_ratio`` PR.

Does **not** merge pricing. Does not change strategy math.

Usage:
  uv run python scripts/c01_promotion_checklist.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_status_line(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("status="):
            continue
        for part in line.split():
            if "=" in part:
                k, _, v = part.partition("=")
                out[k] = v
    return out


def _parse_gate_stdout(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("{") and " " not in line.split("=", 1)[0]:
            k, _, v = line.partition("=")
            if k:
                out[k] = v
    return out


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence-pack", default="logs/candidate_evidence_pack.json")
    ap.add_argument("--min-hours", type=float, default=24.0)
    ap.add_argument("--min-quotes", type=int, default=500)
    ap.add_argument("--target-vol", type=float, default=8.0)
    args = ap.parse_args()

    py = sys.executable
    _, gate_out, _ = _run([py, "scripts/paper_data_gate.py"])
    gate = _parse_gate_stdout(gate_out)

    _, _, health_err = _run([py, "scripts/paper_health.py"])
    health = _parse_status_line(health_err)

    _, _, outage_err = _run([py, "scripts/outage_window_report.py"])
    outage = _parse_status_line(outage_err)

    _, _, cf_err = _run([
        py,
        "scripts/trending_counterfactual.py",
        "--sweep-vol",
        str(args.target_vol),
        "--by-market",
    ])
    cf = _parse_status_line(cf_err)

    pack = _load_json(Path(args.evidence_pack))
    c01 = (pack or {}).get("c01_trend_vol_ratio") or {}
    markets = c01.get("markets") or []
    any_oos = bool(c01.get("any_oos_replicated"))
    thin_any = any(bool(m.get("thin_holdout")) for m in markets) if markets else True
    skipped_validate = bool(c01.get("skipped_validate"))
    pack_present = pack is not None
    validate_present = bool(markets) and not skipped_validate

    try:
        runtime_h = float(str(gate.get("runtime_hours") or "0").split()[0])
    except ValueError:
        runtime_h = 0.0
    try:
        quotes = float(str(gate.get("quotes_for_gate") or "0").split()[0])
    except ValueError:
        quotes = 0.0

    hours_ok = runtime_h >= args.min_hours
    quotes_ok = quotes >= args.min_quotes
    health_ok = health.get("status") == "OK"
    outage_open = str(outage.get("open", "")).lower() == "true"
    cf_ok = cf.get("status") == "OK"

    checks = {
        "hours_ok": hours_ok,
        "quotes_ok": quotes_ok,
        "health_ok": health_ok,
        "outage_closed": not outage_open,
        "counterfactual_ran": cf_ok,
        "evidence_pack_present": pack_present,
        "oos_validate_present": validate_present,
        "oos_replicated": any_oos if validate_present else False,
        "holdout_not_thin": (not thin_any) if validate_present else False,
    }
    blockers = [k for k, v in checks.items() if not v]
    ready = not blockers

    report = {
        "ready_for_tier2_pr": ready,
        "blockers": blockers,
        "checks": checks,
        "gate": {
            "runtime_hours": gate.get("runtime_hours"),
            "runtime_basis": gate.get("runtime_basis"),
            "quotes_for_gate": gate.get("quotes_for_gate"),
            "tier2_allowed": gate.get("tier2_allowed"),
            "reason": gate.get("reason"),
        },
        "health": health.get("status"),
        "outage_open": outage_open,
        "outage_total_h": outage.get("total_h"),
        "counterfactual_line": next(
            (ln for ln in cf_err.splitlines() if ln.startswith("status=")), None
        ),
        "evidence_pack": {
            "path": args.evidence_pack if pack else None,
            "any_oos_replicated": any_oos if validate_present else None,
            "thin_holdout_any": thin_any if validate_present else None,
            "skipped_validate": skipped_validate,
            "n_markets": len(markets),
            "has_counterfactual": bool((pack or {}).get("c01_counterfactual")),
        },
        "target_trend_vol_ratio": args.target_vol,
        "min_hours": args.min_hours,
        "min_quotes": args.min_quotes,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    print(
        f"status={'READY' if ready else 'BLOCKED'} "
        f"blockers={','.join(blockers) or '-'} "
        f"runtime_h={gate.get('runtime_hours')} quotes={gate.get('quotes_for_gate')} "
        f"health={health.get('status')} outage_open={outage_open} "
        f"oos={any_oos if validate_present else None} "
        f"thin={thin_any if validate_present else None}",
        file=sys.stderr,
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
