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
        "hours_to_tier2_gate": 15.63,
        "runtime_h": 8.37,
        "quotes": 5529,
        "connectivity": "status=DOWN",
        "tier2_allowed": False,
        "gate_reason": "need_hours>=24.0",
        "runtime_basis": "requote",
        "tape_frozen": True,
        "eta_paused": True,
        "last_requote_age_s": 28000.0,
        "health": "STALE",
        "ensure_status": "NEEDS_RESTART",
        "collector_pid": 78216,
        "deps_ok": True,
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
