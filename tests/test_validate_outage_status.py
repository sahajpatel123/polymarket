"""Tests for validate_outage_status."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_outage_status import validate_status

_CRITICAL_CMD = "uv run python scripts/await_polymarket_recovery.py --once"
_DIAGNOSE_CMD = (
    "uv run python scripts/await_polymarket_recovery.py --once "
    "--no-restart-on-recover --no-append-cycle-on-recover "
    "--no-smoke-on-recover"
)
_GATE_CMD = "uv run python scripts/paper_data_gate.py"


def _full(**overrides):
    base = {
        "ts": "2026-07-22T21:00:00+00:00",
        "outage_open": True,
        "outage_total_h": 6.5,
        "outage_alert": True,
        "outage_alert_severe": True,
        "outage_alert_prolonged": False,
        "outage_alert_critical": False,
        "outage_alert_imminent": False,
        "outage_alert_final": False,
        "outage_alert_critical_aged": False,
        "outage_alert_critical_hour": False,
        "operator_mode": "OUTAGE_OPEN",
        "operator_action": "await_UP_diagnose_only",
        "operator_recovery_cmd": _DIAGNOSE_CMD,
        "outage_imminent_since": None,
        "hours_in_imminent": None,
        "hours_to_tier2_gate": 15.63,
        "hours_to_critical": 2.66,
        "minutes_to_critical": 160,
        "hours_to_imminent": 1.66,
        "outage_started_at": "2026-07-22T15:30:00+00:00",
        "outage_critical_at": "2026-07-23T03:30:00+00:00",
        "outage_critical_since": None,
        "hours_past_critical": None,
        "minutes_past_critical": None,
        "runtime_h": 8.37,
        "quotes": 5529,
        "connectivity": "status=DOWN",
        "tier2_allowed": False,
        "gate_reason": "need_hours>=24.0",
        "runtime_basis": "requote",
        "tape_frozen": True,
        "eta_paused": True,
        "last_requote_age_s": 28000.0,
        "last_requote_at": "2026-07-22T13:13:20+00:00",
        "health": "STALE",
        "ensure_status": "NEEDS_RESTART",
        "collector_pid": 78216,
        "deps_ok": True,
        "n_cycles": 79,
        "c01_status": "BLOCKED",
        "c01_blockers": "hours_ok,health_ok,outage_closed",
        "paper_log": "livecfg/logs/paper.jsonl.2026-07-22",
        "paper_log_files": 2,
        "metrics_log": "livecfg/logs/metrics-paper.jsonl",
        "recovery_smoke": "FAIL",
        "recovery_smoke_blockers": "connectivity_up",
        "frozen_tape_snapshot": None,
        "frozen_tape_status": "INACTIVE",
    }
    base.update(overrides)
    return base


def test_validate_status_ok() -> None:
    from datetime import datetime

    ts = datetime.fromisoformat("2026-07-22T21:00:00+00:00").timestamp()
    rep = validate_status(_full(), max_age_s=120, now=ts + 30)
    assert rep["ok"] is True
    assert rep["missing"] == []
    assert rep["stale"] is False
    assert rep["recommended_missing"] == []


def test_validate_open_outage_requires_started_at() -> None:
    rep = validate_status(_full(
        outage_started_at=None,
        hours_to_critical=None,
        minutes_to_critical=None,
        outage_critical_at=None,
    ))
    assert rep["ok"] is False
    assert "outage_started_at" in rep["missing"]
    assert "hours_to_critical" in rep["missing"]
    assert "minutes_to_critical" in rep["missing"]
    assert "outage_critical_at" in rep["missing"]
    # Closed outages do not require the open-window fields.
    data = _full(
        outage_open=False,
        outage_alert=False,
        outage_alert_severe=False,
        outage_alert_prolonged=False,
        outage_alert_critical=False,
        operator_mode="QUIET",
        operator_action="continue_paper_gate",
        operator_recovery_cmd=_GATE_CMD,
        tape_frozen=False,
        eta_paused=False,
        health="OK",
        ensure_status="OK",
    )
    data.pop("outage_started_at", None)
    data.pop("hours_to_critical", None)
    data.pop("minutes_to_critical", None)
    data.pop("hours_to_imminent", None)
    data.pop("outage_critical_at", None)
    closed = validate_status(data)
    assert closed["ok"] is True
    assert "outage_started_at" not in closed["missing"]


def test_validate_imminent_requires_since() -> None:
    rep = validate_status(_full(
        outage_alert_imminent=True,
        outage_imminent_since=None,
        hours_in_imminent=None,
        hours_to_critical=0.5,
        hours_to_imminent=0.0,
    ))
    assert rep["ok"] is False
    assert "outage_imminent_since" in rep["missing"]
    assert "hours_in_imminent" in rep["missing"]
    missing_hours = validate_status(_full(
        outage_alert_imminent=True,
        outage_imminent_since="2026-07-23T02:30:25+00:00",
        hours_in_imminent=None,
        hours_to_critical=0.5,
        hours_to_imminent=0.0,
    ))
    assert missing_hours["ok"] is False
    assert "hours_in_imminent" in missing_hours["missing"]
    ok = validate_status(_full(
        outage_alert_imminent=True,
        outage_imminent_since="2026-07-23T02:30:25+00:00",
        hours_in_imminent=0.3,
        hours_to_critical=0.5,
        hours_to_imminent=0.0,
    ))
    assert ok["ok"] is True


def test_validate_critical_requires_since() -> None:
    rep = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_total_h=12.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since=None,
        hours_past_critical=None,
        minutes_past_critical=None,
    ))
    assert rep["ok"] is False
    assert "outage_critical_since" in rep["missing"]
    assert "hours_past_critical" in rep["missing"]
    assert "minutes_past_critical" in rep["missing"]
    ok = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_total_h=12.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=0.2,
        minutes_past_critical=12,
    ))
    assert ok["ok"] is True
    assert ok["inconsistencies"] == []


def test_validate_critical_state_consistency() -> None:
    rep = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=True,
        outage_alert_final=True,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_imminent_since="2026-07-23T02:30:00+00:00",
        hours_in_imminent=1.0,
        outage_total_h=12.1,
        hours_to_critical=0.5,
        minutes_to_critical=30,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=0.0,
        minutes_past_critical=0,
    ))
    assert rep["ok"] is False
    assert "imminent_while_critical" in rep["inconsistencies"]
    assert "final_while_critical" in rep["inconsistencies"]
    assert "minutes_to_critical_nonzero" in rep["inconsistencies"]
    assert "hours_to_critical_nonzero" in rep["inconsistencies"]


def test_validate_operator_mode_consistency() -> None:
    bad = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=True,
        outage_alert_critical_hour=False,
        operator_mode="OUTAGE_OPEN",
        operator_action="await_UP_diagnose_only",
        outage_total_h=12.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=0.5,
        minutes_past_critical=30,
    ))
    assert bad["ok"] is False
    assert "operator_mode_mismatch" in bad["inconsistencies"]
    assert "operator_action_mismatch" in bad["inconsistencies"]
    ok = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=True,
        outage_alert_critical_hour=False,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_total_h=12.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=0.5,
        minutes_past_critical=30,
    ))
    assert ok["ok"] is True
    assert ok["inconsistencies"] == []


def test_validate_critical_aged_hour_consistency() -> None:
    bad = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=False,
        outage_alert_critical_hour=False,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_total_h=13.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=1.1,
        minutes_past_critical=70,
    ))
    assert bad["ok"] is False
    assert "critical_aged_mismatch" in bad["inconsistencies"]
    assert "critical_hour_mismatch" in bad["inconsistencies"]
    hour_only = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=False,
        outage_alert_critical_hour=True,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_total_h=13.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=1.1,
        minutes_past_critical=70,
    ))
    assert "hour_without_aged" in hour_only["inconsistencies"]
    ok = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=True,
        outage_alert_critical_hour=True,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=_CRITICAL_CMD,
        outage_total_h=13.1,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=1.1,
        minutes_past_critical=70,
    ))
    assert ok["ok"] is True
    assert ok["inconsistencies"] == []


def test_validate_operator_recovery_cmd_consistency() -> None:
    bad = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=True,
        outage_alert_critical_hour=True,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd="uv run python scripts/paper_data_gate.py",
        outage_total_h=13.5,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=1.5,
        minutes_past_critical=90,
    ))
    assert bad["ok"] is False
    assert "operator_recovery_cmd_mismatch" in bad["inconsistencies"]
    ok = validate_status(_full(
        outage_alert_critical=True,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_alert_critical_aged=True,
        outage_alert_critical_hour=True,
        operator_mode="CRITICAL_OPEN",
        operator_action="await_UP_then_full_recovery",
        operator_recovery_cmd=(
            "uv run python scripts/await_polymarket_recovery.py --once"
        ),
        outage_total_h=13.5,
        hours_to_critical=0.0,
        minutes_to_critical=0,
        hours_to_imminent=0.0,
        outage_critical_since="2026-07-23T03:28:00+00:00",
        hours_past_critical=1.5,
        minutes_past_critical=90,
    ))
    assert ok["ok"] is True
    assert ok["inconsistencies"] == []


def test_validate_status_missing_keys() -> None:
    rep = validate_status({"ts": "2026-07-22T21:00:00+00:00", "outage_open": True})
    assert rep["ok"] is False
    assert "hours_to_tier2_gate" in rep["missing"]
    assert "quotes" in rep["missing"]


def test_validate_status_stale_when_open() -> None:
    from datetime import datetime

    ts = datetime.fromisoformat("2026-07-22T21:00:00+00:00").timestamp()
    rep = validate_status(_full(), max_age_s=60, now=ts + 600)
    assert rep["ok"] is False
    assert rep["stale"] is True


def test_validate_cli(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = tmp_path / "outage_status.json"
    path.write_text(json.dumps(_full()) + "\n")
    proc = subprocess.run(
        [sys.executable, "scripts/validate_outage_status.py", "--path", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "status=OK" in proc.stderr
