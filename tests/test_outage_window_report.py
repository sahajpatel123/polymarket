"""Tests for strategy-cycle outage window report."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.outage_window_report import analyze_cycles, compact_status, write_compact_status


def test_analyze_cycles_detects_open_stale_window() -> None:
    rows = [
        {
            "ts": "2026-07-22T10:00:00+00:00",
            "health": {"status": "OK"},
            "gate": {"runtime_hours": "8.0", "quotes_for_gate": "5000"},
        },
        {
            "ts": "2026-07-22T10:10:00+00:00",
            "health": {"status": "STALE"},
            "connectivity": {"status": "DOWN"},
            "gate": {"runtime_hours": "8.1", "quotes_for_gate": "5000"},
        },
        {
            "ts": "2026-07-22T10:20:00+00:00",
            "health": {"status": "STALE"},
            "connectivity": {"status": "DOWN"},
            "gate": {"runtime_hours": "8.1", "quotes_for_gate": "5000"},
        },
    ]
    rep = analyze_cycles(rows)
    assert rep["n_outage_windows"] == 1
    assert rep["outage_open"] is True
    assert rep["current"]["quotes_start"] == 5000.0
    assert rep["current"]["n_cycles"] == 2


def test_compact_status_alert_thresholds() -> None:
    mild = compact_status({"outage_open": True, "outage_total_h": 3.5, "n_outage_windows": 1, "current": {}})
    assert mild["outage_alert"] is True
    assert mild["outage_alert_severe"] is False
    assert mild["outage_alert_prolonged"] is False
    assert mild["outage_alert_critical"] is False
    assert mild["hours_to_critical"] == 8.5
    assert mild["hours_to_tier2_gate"] is None
    severe = compact_status({
        "outage_open": True,
        "outage_total_h": 5.5,
        "n_outage_windows": 1,
        "current": {"runtime_hours_end": 8.37, "quotes_end": 5529},
    })
    assert severe["outage_alert_severe"] is True
    assert severe["outage_alert_prolonged"] is False
    assert severe["hours_to_tier2_gate"] == 15.63
    assert severe["hours_to_critical"] == 6.5
    assert severe["quotes"] == 5529
    assert isinstance(severe["quotes"], int)
    prolonged = compact_status({
        "outage_open": True,
        "outage_total_h": 8.01,
        "n_outage_windows": 1,
        "current": {"runtime_hours_end": 8.37, "quotes_end": 5529.0},
    })
    assert prolonged["outage_alert_prolonged"] is True
    assert prolonged["outage_alert_critical"] is False
    assert prolonged["hours_to_critical"] == 3.99
    assert prolonged["quotes"] == 5529
    critical = compact_status({
        "outage_open": True,
        "outage_total_h": 12.01,
        "n_outage_windows": 1,
        "current": {"runtime_hours_end": 8.37, "quotes_end": "5529"},
    })
    assert critical["outage_alert_critical"] is True
    assert critical["hours_to_critical"] == 0.0
    assert critical["quotes"] == 5529
    gated = compact_status({
        "outage_open": False,
        "outage_total_h": 0.0,
        "n_outage_windows": 0,
        "current": {"runtime_hours_end": 24.5},
    })
    assert gated["hours_to_tier2_gate"] == 0.0
    assert gated["hours_to_critical"] == 12.0


def test_write_compact_status_preserves_probe_fields(tmp_path: Path) -> None:
    path = tmp_path / "outage_status.json"
    path.write_text(json.dumps({
        "connectivity": "status=DOWN",
        "recovered": False,
        "tier2_allowed": False,
        "gate_reason": "need_hours>=24.0",
    }) + "\n")
    written = write_compact_status(path, {
        "outage_open": True,
        "outage_total_h": 6.0,
        "hours_to_tier2_gate": 15.63,
    })
    assert written["connectivity"] == "status=DOWN"
    assert written["recovered"] is False
    assert written["tier2_allowed"] is False
    assert written["gate_reason"] == "need_hours>=24.0"
    assert written["outage_open"] is True


def test_outage_window_report_cli(tmp_path: Path) -> None:
    log = tmp_path / "cycles.jsonl"
    rows = [
        {
            "ts": "2026-07-22T10:00:00+00:00",
            "health": {"status": "OK"},
            "gate": {"runtime_hours": "1.0", "quotes_for_gate": "100"},
        },
        {
            "ts": "2026-07-22T10:30:00+00:00",
            "health": {"status": "STALE"},
            "connectivity": {"status": "DOWN"},
            "gate": {"runtime_hours": "1.0", "quotes_for_gate": "100"},
        },
        {
            "ts": "2026-07-22T11:00:00+00:00",
            "health": {"status": "OK"},
            "connectivity": {"status": "OK"},
            "gate": {"runtime_hours": "1.5", "quotes_for_gate": "200"},
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    proc = subprocess.run(
        [sys.executable, "scripts/outage_window_report.py", "--log", str(log)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["n_outage_windows"] == 1
    assert payload["outage_open"] is False
    assert payload["windows"][0]["duration_s"] == 1800.0
    assert "status=OK" in proc.stderr
