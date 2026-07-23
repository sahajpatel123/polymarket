#!/usr/bin/env python3
"""One-shot strategy-loop tick: connectivity + C-01 + summarize (+ optional append).

Tier-1 ops helper for Agent-1 10m cycles. Does not change strategy math.

Default is diagnose-only (no collector restart, no cycle append). Pass
``--append`` to record a trail row; recovery relaunch stays on
``await_polymarket_recovery.py``.

Usage:
  uv run python scripts/strategy_tick.py
  uv run python scripts/strategy_tick.py --append --skip-connectivity
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _status_line(stderr: str, stdout: str = "") -> str:
    for line in stderr.splitlines() + stdout.splitlines():
        if line.startswith("status="):
            return line
    return "status=UNKNOWN"


def _parse_kv(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in line.split():
        if "=" in part:
            k, _, v = part.partition("=")
            out[k] = v
    return out


def _parse_gate_stdout(stdout: str) -> dict[str, Any]:
    """Pull Tier-2 gate fields from paper_data_gate stdout (T1-80/T1-97/T1-99)."""
    kv: dict[str, str] = {}
    wanted = {
        "tier2_allowed",
        "reason",
        "runtime_basis",
        "runtime_hours",
        "quotes_for_gate",
        "status",
        "log_path",
        "metrics_path",
        "log_files",
    }
    for line in stdout.splitlines():
        if line.startswith(" ") or line.startswith("{"):
            continue
        # Paths may contain '=' only after the first key=; use partition once per line
        # for dedicated path lines, otherwise token-split.
        if line.startswith("log_path=") or line.startswith("metrics_path="):
            k, _, v = line.partition("=")
            kv[k] = v
            continue
        for part in line.split():
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            if k in wanted:
                kv[k] = v
    out: dict[str, Any] = {}
    if "tier2_allowed" in kv:
        out["tier2_allowed"] = kv["tier2_allowed"].lower() == "true"
    if "reason" in kv:
        out["gate_reason"] = kv["reason"]
    if "runtime_basis" in kv:
        out["runtime_basis"] = kv["runtime_basis"]
    if "runtime_hours" in kv:
        try:
            out["gate_runtime_h"] = float(kv["runtime_hours"])
        except ValueError:
            out["gate_runtime_h"] = kv["runtime_hours"]
    if "quotes_for_gate" in kv:
        try:
            out["gate_quotes"] = int(float(kv["quotes_for_gate"]))
        except ValueError:
            out["gate_quotes"] = kv["quotes_for_gate"]
    if "status" in kv:
        out["gate_status"] = kv["status"]
    if "log_path" in kv:
        out["paper_log"] = kv["log_path"]
    if "metrics_path" in kv:
        out["metrics_log"] = kv["metrics_path"]
    if "log_files" in kv:
        try:
            out["paper_log_files"] = int(kv["log_files"])
        except ValueError:
            out["paper_log_files"] = kv["log_files"]
    # Prefer live gate values for the canonical outage_status keys (T1-99).
    if "gate_quotes" in out:
        out["quotes"] = out["gate_quotes"]
    if "gate_runtime_h" in out:
        out["runtime_h"] = out["gate_runtime_h"]
        try:
            rh = float(out["gate_runtime_h"])
            out["hours_to_tier2_gate"] = round(max(0.0, 24.0 - rh), 2)
        except (TypeError, ValueError):
            pass
    return out


def _merge_outage_status(path: Path, fields: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    data.update({"ts": datetime.now(timezone.utc).isoformat(), **fields})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def _coerce_status_value(raw: str) -> Any:
    low = raw.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"none", "null"}:
        return None
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _summarize_freeze_fields(sm: dict[str, Any]) -> dict[str, Any]:
    """Pull tape-freeze fields from summarize status into outage_status (T1-86)."""
    out: dict[str, Any] = {}
    for key in ("tape_frozen", "eta_paused", "last_requote_age_s"):
        if key in sm and sm[key] not in (None, ""):
            out[key] = _coerce_status_value(str(sm[key]))
    # Longitudinal trail length (T1-94).
    if sm.get("cycles") not in (None, ""):
        out["n_cycles"] = _coerce_status_value(str(sm["cycles"]))
    return out


def _live_health_fields(health: dict[str, Any]) -> dict[str, Any]:
    """Prefer live paper_health ages over stale cycle-trail values (T1-87/T1-105)."""
    out: dict[str, Any] = {}
    status = str(health.get("status") or "")
    if status in {"OK", "STALE"}:
        out["health"] = status
        out["tape_frozen"] = status == "STALE"
    now = datetime.now(timezone.utc)
    if health.get("last_requote_age_s") not in (None, ""):
        age = _coerce_status_value(str(health["last_requote_age_s"]))
        out["last_requote_age_s"] = age
        try:
            age_f = float(age)
            out["last_requote_at"] = datetime.fromtimestamp(
                now.timestamp() - age_f, tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    if health.get("last_quote_age_s") not in (None, ""):
        age = _coerce_status_value(str(health["last_quote_age_s"]))
        out["last_quote_age_s"] = age
        try:
            age_f = float(age)
            out["last_quote_at"] = datetime.fromtimestamp(
                now.timestamp() - age_f, tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    return out


def _ensure_collector_fields(ensure: dict[str, Any]) -> dict[str, Any]:
    """Pull diagnose-only collector pid/status into outage_status (T1-90)."""
    out: dict[str, Any] = {}
    status = ensure.get("status")
    if status:
        out["ensure_status"] = status
    pids = ensure.get("pids")
    if pids not in (None, ""):
        # status line has pids=[78216]
        raw = str(pids).strip()
        out["collector_pids"] = raw
        try:
            cleaned = raw.strip("[]")
            if cleaned:
                out["collector_pid"] = int(cleaned.split(",")[0].strip())
        except ValueError:
            pass
    return out


def _c01_blocker_fields(c01: dict[str, Any]) -> dict[str, Any]:
    """Persist C-01 promotion blockers on the compact status (T1-95)."""
    out: dict[str, Any] = {}
    status = c01.get("status")
    if status:
        out["c01_status"] = status
    blockers = c01.get("blockers")
    if blockers not in (None, ""):
        out["c01_blockers"] = blockers
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--append",
        action="store_true",
        help="Also run append_strategy_cycle (records trail row)",
    )
    ap.add_argument(
        "--skip-connectivity",
        action="store_true",
        help="Skip Polymarket probe (faster during known outages)",
    )
    ap.add_argument(
        "--with-counterfactual",
        action="store_true",
        help="When --append, also record offline C-01 counterfactual",
    )
    ap.add_argument(
        "--write-weekly",
        action="store_true",
        help="Also overwrite WEEKLY_REPORT.md from live script outputs",
    )
    args = ap.parse_args()
    py = sys.executable
    status_path = Path("logs/outage_status.json")
    report: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    if not args.skip_connectivity:
        code, out, err = _run([
            py,
            "scripts/await_polymarket_recovery.py",
            "--once",
            "--no-restart-on-recover",
            "--no-append-cycle-on-recover",
            "--no-smoke-on-recover",
        ])
        line = _status_line(err, out)
        report["steps"]["connectivity"] = {
            "rc": code,
            "status_line": line,
            **_parse_kv(line),
        }
    else:
        report["steps"]["connectivity"] = {"status": "SKIPPED"}

    code, out, err = _run([py, "scripts/c01_promotion_checklist.py"])
    line = _status_line(err, out)
    report["steps"]["c01"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    code, hout, herr = _run([py, "scripts/paper_health.py"])
    hline = _status_line(herr, hout)
    hkv = _parse_kv(hline)
    report["steps"]["health"] = {"rc": code, "status_line": hline, **hkv}

    code, eout, eerr = _run([
        py,
        "scripts/ensure_paper_collector.py",
        "--config-dir",
        "livecfg",
    ])
    eline = _status_line(eerr, eout)
    report["steps"]["ensure"] = {"rc": code, "status_line": eline, **_parse_kv(eline)}

    code, out, err = _run([py, "scripts/unused_knob_toml_scan.py"])
    line = _status_line(err, out)
    report["steps"]["unused_knobs"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    code, out, err = _run([
        py,
        "scripts/outage_window_report.py",
        "--status-out",
        str(status_path),
    ])
    line = _status_line(err, out)
    report["steps"]["outage"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    code, gout, gerr = _run([py, "scripts/paper_data_gate.py"])
    gate_fields = _parse_gate_stdout(gout)
    report["steps"]["gate"] = {
        "rc": code,
        "status_line": _status_line(gerr, gout),
        **{k: str(v) for k, v in gate_fields.items()},
    }

    # Merge live probe/gate/health first so summarize can read a fresh
    # outage_status.json for hours_to_critical / outage_started_at (T1-102).
    merge_fields: dict[str, Any] = dict(gate_fields)
    merge_fields.update(_live_health_fields(report["steps"].get("health") or {}))
    merge_fields.update(_ensure_collector_fields(report["steps"].get("ensure") or {}))
    merge_fields.update(_c01_blocker_fields(report["steps"].get("c01") or {}))
    conn = report["steps"].get("connectivity") or {}
    conn_line = str(conn.get("status_line") or "")
    if conn_line and conn.get("status") != "SKIPPED":
        if conn.get("rest_ok") is not None:
            merge_fields["connectivity"] = (
                f"status={conn.get('status')} "
                f"rest_ok={conn.get('rest_ok')} "
                f"ws_ok={conn.get('ws_ok')}"
            )
        else:
            merge_fields["connectivity"] = conn_line
        merge_fields["recovered"] = "RECOVERED" in conn_line
    if merge_fields:
        _merge_outage_status(status_path, merge_fields)

    code, out, err = _run([py, "scripts/summarize_strategy_cycles.py"])
    line = _status_line(err, out)
    report["steps"]["summarize"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    # Second merge: trail-derived freeze / n_cycles after summarize.
    freeze = _summarize_freeze_fields(report["steps"].get("summarize") or {})
    if freeze:
        _merge_outage_status(status_path, freeze)

    code, vout, verr = _run([
        py,
        "scripts/validate_outage_status.py",
        "--path",
        str(status_path),
        "--max-age-s",
        "900",
    ])
    line = _status_line(verr, vout)
    report["steps"]["outage_status_validate"] = {
        "rc": code,
        "status_line": line,
        **_parse_kv(line),
    }

    code, dout, derr = _run([py, "scripts/deps_audit.py"])
    dline = _status_line(derr, dout)
    deps_ok = None
    deps_bumps = None
    deps_flagged = None
    try:
        djson = json.loads(dout) if dout.strip().startswith("{") else {}
        deps_ok = djson.get("ok")
        deps_bumps = len(djson.get("baseline_bumps") or [])
        deps_flagged = len(djson.get("flagged") or [])
    except json.JSONDecodeError:
        djson = {}
    dkv = _parse_kv(dline)
    report["steps"]["deps"] = {
        "rc": code,
        "status_line": dline,
        "ok": deps_ok,
        "bumps": deps_bumps,
        "flagged": deps_flagged,
        **dkv,
    }
    if status_path.exists() and deps_ok is not None:
        _merge_outage_status(
            status_path,
            {
                "deps_ok": bool(deps_ok),
                "deps_bumps": deps_bumps,
                "deps_flagged": deps_flagged,
            },
        )

    if args.append:
        cmd = [py, "scripts/append_strategy_cycle.py"]
        if args.skip_connectivity:
            cmd.append("--skip-connectivity")
        if args.with_counterfactual:
            cmd.append("--with-counterfactual")
        code, out, err = _run(cmd)
        line = _status_line(err, out)
        report["steps"]["append"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    if args.write_weekly:
        code, out, err = _run([py, "scripts/write_weekly_report.py"])
        line = _status_line(err, out)
        report["steps"]["weekly"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    print(json.dumps(report, indent=2, sort_keys=True))
    conn = report["steps"].get("connectivity") or {}
    c01 = report["steps"].get("c01") or {}
    sm = report["steps"].get("summarize") or {}
    health = report["steps"].get("health") or {}
    ensure = report["steps"].get("ensure") or {}
    unused = report["steps"].get("unused_knobs") or {}
    outage = report["steps"].get("outage") or {}
    gate = report["steps"].get("gate") or {}
    ost_val = report["steps"].get("outage_status_validate") or {}
    deps = report["steps"].get("deps") or {}
    ap_step = report["steps"].get("append") or {}
    weekly = report["steps"].get("weekly") or {}
    # Prefer merged outage_status.json over pre-merge step kv (T1-100).
    ost: dict[str, Any] = {}
    if status_path.exists():
        try:
            ost = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            ost = {}
    conn_line = str(conn.get("status_line") or "")
    if conn.get("status") == "SKIPPED":
        conn_disp = "SKIPPED"
    elif "STILL_DOWN" in conn_line or "TIMEOUT" in conn_line or "RECOVERED" in conn_line or "UP_DIAGNOSE" in conn_line:
        conn_disp = conn_line.split()[0].split("=", 1)[-1]
    else:
        conn_disp = conn.get("status") or conn_line or "UNKNOWN"
    print(
        f"status=OK "
        f"connectivity={conn_disp} "
        f"c01={c01.get('status')} blockers={c01.get('blockers')} "
        f"outage_alert={ost.get('outage_alert', c01.get('outage_alert') or outage.get('outage_alert'))} "
        f"outage_alert_severe={ost.get('outage_alert_severe', c01.get('outage_alert_severe') or outage.get('outage_alert_severe'))} "
        f"outage_alert_prolonged={ost.get('outage_alert_prolonged', c01.get('outage_alert_prolonged') or outage.get('outage_alert_prolonged'))} "
        f"outage_alert_critical={ost.get('outage_alert_critical', c01.get('outage_alert_critical') or outage.get('outage_alert_critical'))} "
        f"outage_alert_imminent={ost.get('outage_alert_imminent')} "
        f"outage_imminent_since={ost.get('outage_imminent_since') or '-'} "
        f"hours_in_imminent={ost.get('hours_in_imminent')} "
        f"outage_total_h={ost.get('outage_total_h', outage.get('total_h'))} "
        f"hours_to_tier2_gate={ost.get('hours_to_tier2_gate', outage.get('hours_to_tier2_gate'))} "
        f"hours_to_critical={ost.get('hours_to_critical')} "
        f"minutes_to_critical={ost.get('minutes_to_critical')} "
        f"hours_to_imminent={ost.get('hours_to_imminent')} "
        f"outage_started_at={ost.get('outage_started_at') or '-'} "
        f"outage_critical_at={ost.get('outage_critical_at') or '-'} "
        f"outage_critical_since={ost.get('outage_critical_since') or '-'} "
        f"hours_past_critical={ost.get('hours_past_critical')} "
        f"quotes={ost.get('quotes', outage.get('quotes'))} "
        f"tier2_allowed={ost.get('tier2_allowed', gate.get('tier2_allowed'))} "
        f"gate_reason={ost.get('gate_reason', gate.get('gate_reason'))} "
        f"outage_status={ost_val.get('status')} "
        f"deps_ok={deps.get('ok')} deps_bumps={deps.get('bumps')} "
        f"health={ost.get('health', health.get('status'))} "
        f"ensure={ost.get('ensure_status', ensure.get('status'))} "
        f"collector_pid={ost.get('collector_pid', ensure.get('pids'))} "
        f"n_cycles={ost.get('n_cycles', sm.get('cycles'))} "
        f"last_requote_age_s={ost.get('last_requote_age_s', health.get('last_requote_age_s') or sm.get('last_requote_age_s'))} "
        f"last_requote_at={ost.get('last_requote_at') or '-'} "
        f"runtime_h={ost.get('runtime_h', sm.get('runtime_h'))} "
        f"eta_paused={ost.get('eta_paused', sm.get('eta_paused'))} "
        f"tape_frozen={ost.get('tape_frozen', sm.get('tape_frozen'))} "
        f"unused_set={unused.get('n_set_unused')} "
        f"append={ap_step.get('status') or ('SKIPPED' if not args.append else 'UNKNOWN')} "
        f"weekly={weekly.get('status') or ('SKIPPED' if not args.write_weekly else 'UNKNOWN')}",
        file=sys.stderr,
    )
    # Non-zero only if checklist crashed (rc not in 0/1) or summarize failed hard.
    bad = False
    if report["steps"]["c01"]["rc"] not in (0, 1):
        bad = True
    if report["steps"]["summarize"]["rc"] != 0:
        bad = True
    if report["steps"]["health"]["rc"] not in (0, 1):
        bad = True
    # ensure: 0=OK, 1=NEEDS_RESTART, 2=SKIPPED_UPSTREAM_DOWN — all informative
    if report["steps"]["ensure"]["rc"] not in (0, 1, 2):
        bad = True
    if report["steps"]["unused_knobs"]["rc"] != 0:
        bad = True
    if report["steps"]["outage"]["rc"] not in (0, 2):
        bad = True
    if report["steps"]["gate"]["rc"] not in (0, 1):
        bad = True
    if report["steps"]["outage_status_validate"]["rc"] not in (0, 1, 2):
        bad = True
    if report["steps"]["deps"]["rc"] != 0:
        bad = True
    if args.append and report["steps"]["append"]["rc"] != 0:
        bad = True
    if args.write_weekly and report["steps"]["weekly"]["rc"] != 0:
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
