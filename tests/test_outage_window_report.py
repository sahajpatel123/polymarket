"""Tests for strategy-cycle outage window report."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.outage_window_report import analyze_cycles, compact_status


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
    severe = compact_status({"outage_open": True, "outage_total_h": 5.5, "n_outage_windows": 1, "current": {}})
    assert severe["outage_alert_severe"] is True


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
