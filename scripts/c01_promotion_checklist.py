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

    _, _, regime_err = _run([py, "scripts/paper_regime_report.py"])
    regime = _parse_status_line(regime_err)

    def _f(key: str) -> float | None:
        raw = regime.get(key)
        if raw is None or raw in ("", "None", "null"):
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    suggested = _f("suggested_vol")
    sweep_vals: list[float] = []
    if suggested is not None:
        sweep_vals.append(suggested)
    if args.target_vol not in sweep_vals:
        sweep_vals.append(float(args.target_vol))
    # Include live default (2.0) when it differs — baseline for C-01 delta.
    if 2.0 not in sweep_vals:
        sweep_vals.insert(0, 2.0)

    _, cf_out, cf_err = _run([
        py,
        "scripts/trending_counterfactual.py",
        "--sweep-vol",
        ",".join(str(v) for v in sweep_vals),
        "--by-market",
    ])
    cf = _parse_status_line(cf_err)
    cf_sweep_all: list[dict[str, Any]] = []
    try:
        cf_obj = json.loads(cf_out) if cf_out.strip().startswith("{") else {}
        for m in cf_obj.get("markets") or []:
            if m.get("condition_id") == "ALL":
                cf_sweep_all = m.get("sweep") or []
                break
        if not cf_sweep_all and cf_obj.get("markets"):
            # by-market mode: average suppress_frac across markets per vol level
            by_vol: dict[float, list[float]] = {}
            for m in cf_obj["markets"]:
                for row in m.get("sweep") or []:
                    v = float(row.get("candidate_trend_vol_ratio") or 0)
                    by_vol.setdefault(v, []).append(float(row.get("would_suppress_frac") or 0))
            cf_sweep_all = [
                {
                    "candidate_trend_vol_ratio": v,
                    "would_suppress_frac": round(sum(xs) / len(xs), 6) if xs else 0.0,
                    "markets_n": len(xs),
                }
                for v, xs in sorted(by_vol.items())
            ]
    except (json.JSONDecodeError, TypeError, ValueError):
        cf_sweep_all = []

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

    quiet_max = _f("quiet_vol_max")
    trend_min = _f("trend_vol_min")
    vol_gap = _f("vol_gap")
    target_above_quiet = (
        None if quiet_max is None else args.target_vol > quiet_max
    )
    target_above_trend_min = (
        None if trend_min is None else args.target_vol > trend_min
    )
    # Gap under 0.25 means default 2.0 sits near the QUIET/TRENDING boundary.
    boundary_tight = None if vol_gap is None else abs(vol_gap) < 0.25

    def _suppress_at(vol: float | None) -> float | None:
        if vol is None or not cf_sweep_all:
            return None
        for row in cf_sweep_all:
            try:
                if abs(float(row.get("candidate_trend_vol_ratio")) - float(vol)) < 1e-6:
                    return float(row.get("would_suppress_frac"))
            except (TypeError, ValueError):
                continue
        return None

    suppress_at_2 = _suppress_at(2.0)
    suppress_at_suggested = _suppress_at(suggested)
    suppress_at_target = _suppress_at(float(args.target_vol))

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
        "last_requote_age_s": health.get("last_requote_age_s"),
        "outage_open": outage_open,
        "outage_total_h": outage.get("total_h"),
        "outage_alert": (
            None
            if outage.get("total_h") in (None, "")
            else float(str(outage.get("total_h")).split()[0]) >= 3.0
        ),
        "counterfactual_line": next(
            (ln for ln in cf_err.splitlines() if ln.startswith("status=")), None
        ),
        "vol_context": {
            "quiet_vol_max": quiet_max,
            "quiet_vol_p90": _f("quiet_vol_p90"),
            "trend_vol_min": trend_min,
            "trend_vol_p50": _f("trend_vol_p50"),
            "vol_gap": vol_gap,
            "suggested_trend_vol_ratio": suggested,
            "false_trending_attr_frac": _f("false_trending_attr_frac"),
            "target_trend_vol_ratio": args.target_vol,
            "target_above_quiet_max": target_above_quiet,
            "target_above_trend_min": target_above_trend_min,
            "boundary_tight": boundary_tight,
            "cf_sweep_vols": sweep_vals,
            "suppress_frac_at_2": suppress_at_2,
            "suppress_frac_at_suggested": suppress_at_suggested,
            "suppress_frac_at_target": suppress_at_target,
            "cf_sweep": cf_sweep_all,
        },
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
        f"health={health.get('status')} last_requote_age_s={health.get('last_requote_age_s')} "
        f"outage_open={outage_open} outage_total_h={outage.get('total_h')} "
        f"outage_alert={report['outage_alert']} "
        f"oos={any_oos if validate_present else None} "
        f"thin={thin_any if validate_present else None} "
        f"vol_gap={vol_gap} quiet_vol_max={quiet_max} trend_vol_min={trend_min} "
        f"suggested_vol={suggested} "
        f"suppress_2={suppress_at_2} suppress_suggested={suppress_at_suggested} "
        f"suppress_target={suppress_at_target} "
        f"false_trending_attr_frac={_f('false_trending_attr_frac')} "
        f"boundary_tight={boundary_tight}",
        file=sys.stderr,
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
