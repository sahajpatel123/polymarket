"""Tests for strategy cycle history summary / ETA."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_summarize_strategy_cycles_eta(tmp_path: Path) -> None:
    log = tmp_path / "cycles.jsonl"
    rows = [
        {
            "ts": "2026-07-22T08:00:00+00:00",
            "gate": {"runtime_hours": "1.0", "quotes_for_gate": "500"},
            "health": {"status": "OK"},
            "rank": {"spearman": "-1.0"},
            "shadow": {"lifetimes": "100", "crossed_frac": "0.0", "markout_30s": "0.001"},
        },
        {
            "ts": "2026-07-22T09:00:00+00:00",
            "gate": {"runtime_hours": "2.0", "quotes_for_gate": "900"},
            "health": {"status": "OK"},
            "rank": {"spearman": "-1.0"},
            "shadow": {"lifetimes": "200", "crossed_frac": "0.0", "markout_30s": "-0.002"},
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    proc = subprocess.run(
        [sys.executable, "scripts/summarize_strategy_cycles.py", "--log", str(log), "--min-hours", "24"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["n_cycles"] == 2
    assert payload["hours_remaining"] == 22.0
    # 1 paper-hour per wall-hour → 22h ETA
    assert payload["eta_wall_hours_to_gate"] == 22.0
    assert payload["eta_paused"] is False
    assert payload["last_markout_30s"] == "-0.002"
    assert "status=OK" in proc.stderr
    assert "markout_30s=-0.002" in proc.stderr


def test_summarize_strategy_cycles_eta_paused_when_stale(tmp_path: Path) -> None:
    log = tmp_path / "cycles.jsonl"
    rows = [
        {
            "ts": "2026-07-22T08:00:00+00:00",
            "gate": {"runtime_hours": "1.0", "quotes_for_gate": "500"},
            "health": {"status": "OK"},
        },
        {
            "ts": "2026-07-22T09:00:00+00:00",
            "gate": {"runtime_hours": "2.0", "quotes_for_gate": "900"},
            "health": {"status": "STALE"},
            "connectivity": {"status": "DOWN"},
        },
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    proc = subprocess.run(
        [sys.executable, "scripts/summarize_strategy_cycles.py", "--log", str(log), "--min-hours", "24"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["eta_paused"] is True
    assert payload["eta_wall_hours_to_gate"] is None
    assert "eta_paused=True" in proc.stderr
    assert "connectivity=DOWN" in proc.stderr


def test_summarize_overlays_live_outage_status(tmp_path: Path) -> None:
    log = tmp_path / "cycles.jsonl"
    ost = tmp_path / "outage_status.json"
    rows = [
        {
            "ts": "2026-07-22T09:00:00+00:00",
            "gate": {"runtime_hours": "8.0", "quotes_for_gate": "5000"},
            "health": {"status": "STALE"},
            "connectivity": {"status": "DOWN"},
            "outage_status": {
                "hours_to_critical": 5.0,
                "outage_started_at": "stale-from-trail",
            },
        },
    ]
    log.write_text(json.dumps(rows[0]) + "\n")
    ost.write_text(json.dumps({
        "hours_to_critical": 2.32,
        "outage_started_at": "2026-07-22T15:27:53+00:00",
        "outage_open": True,
        "outage_total_h": 9.68,
        "outage_alert_critical": False,
    }) + "\n")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/summarize_strategy_cycles.py",
            "--log",
            str(log),
            "--outage-status",
            str(ost),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["last_hours_to_critical"] == 2.32
    assert payload["last_outage_started_at"] == "2026-07-22T15:27:53+00:00"
    assert "hours_to_critical=2.32" in proc.stderr
    assert "outage_started_at=2026-07-22T15:27:53+00:00" in proc.stderr
