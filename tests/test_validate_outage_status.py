"""Tests for validate_outage_status."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_outage_status import validate_status


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
        "outage_imminent_since": None,
        "hours_to_tier2_gate": 15.63,
        "hours_to_critical": 2.66,
        "hours_to_imminent": 1.66,
        "outage_started_at": "2026-07-22T15:30:00+00:00",
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
    rep = validate_status(_full(outage_started_at=None, hours_to_critical=None))
    assert rep["ok"] is False
    assert "outage_started_at" in rep["missing"]
    assert "hours_to_critical" in rep["missing"]
    # Closed outages do not require the open-window fields.
    data = _full(
        outage_open=False,
        outage_alert=False,
        outage_alert_severe=False,
        outage_alert_prolonged=False,
        outage_alert_critical=False,
        tape_frozen=False,
        eta_paused=False,
        health="OK",
        ensure_status="OK",
    )
    data.pop("outage_started_at", None)
    data.pop("hours_to_critical", None)
    data.pop("hours_to_imminent", None)
    closed = validate_status(data)
    assert closed["ok"] is True
    assert "outage_started_at" not in closed["missing"]


def test_validate_imminent_requires_since() -> None:
    rep = validate_status(_full(
        outage_alert_imminent=True,
        outage_imminent_since=None,
        hours_to_critical=0.5,
        hours_to_imminent=0.0,
    ))
    assert rep["ok"] is False
    assert "outage_imminent_since" in rep["missing"]
    ok = validate_status(_full(
        outage_alert_imminent=True,
        outage_imminent_since="2026-07-23T02:30:25+00:00",
        hours_to_critical=0.5,
        hours_to_imminent=0.0,
    ))
    assert ok["ok"] is True


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
