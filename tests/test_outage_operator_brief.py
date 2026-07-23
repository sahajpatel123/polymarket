"""Tests for outage_operator_brief."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.outage_operator_brief import operator_brief


def test_operator_brief_critical_open() -> None:
    brief = operator_brief({
        "outage_open": True,
        "outage_alert_critical": True,
        "outage_total_h": 12.3,
        "minutes_past_critical": 20,
        "hours_past_critical": 0.33,
        "quotes": 5529,
        "runtime_h": 8.37,
        "hours_to_tier2_gate": 15.63,
        "connectivity": "status=DOWN rest_ok=False ws_ok=False",
        "c01_status": "BLOCKED",
    })
    assert brief["mode"] == "CRITICAL_OPEN"
    assert brief["action"] == "await_UP_then_full_recovery"
    assert brief["minutes_past_critical"] == 20


def test_operator_brief_outage_open() -> None:
    brief = operator_brief({
        "outage_open": True,
        "outage_alert_critical": False,
        "outage_total_h": 6.0,
        "connectivity": "status=DOWN",
    })
    assert brief["mode"] == "OUTAGE_OPEN"
    assert brief["action"] == "await_UP_diagnose_only"


def test_operator_brief_recovered() -> None:
    brief = operator_brief({
        "outage_open": False,
        "outage_alert_critical": False,
        "recovered": True,
        "connectivity": "status=OK rest_ok=True ws_ok=True",
        "quotes": 5600,
    })
    assert brief["mode"] == "RECOVERED"
    assert brief["action"] == "run_recovery_smoke"


def test_operator_brief_quiet() -> None:
    brief = operator_brief({
        "outage_open": False,
        "outage_alert_critical": False,
        "connectivity": "status=OK rest_ok=True ws_ok=True",
    })
    assert brief["mode"] == "QUIET"
    assert brief["action"] == "continue_paper_gate"


def test_operator_brief_cli(tmp_path: Path) -> None:
    path = tmp_path / "outage_status.json"
    path.write_text(json.dumps({
        "outage_open": True,
        "outage_alert_critical": True,
        "outage_total_h": 12.3,
        "minutes_past_critical": 20,
        "quotes": 5529,
        "c01_status": "BLOCKED",
        "connectivity": "status=DOWN",
    }) + "\n")
    proc = subprocess.run(
        [sys.executable, "scripts/outage_operator_brief.py", "--path", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "CRITICAL_OPEN"
    assert "status=CRITICAL_OPEN" in proc.stderr
    assert "action=await_UP_then_full_recovery" in proc.stderr
