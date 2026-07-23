"""Tests for post-recovery smoke checklist."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.recovery_smoke import evaluate_recovery


def _recovered(**overrides):
    base = {
        "connectivity": "status=OK rest_ok=True ws_ok=True",
        "recovered": True,
        "outage_open": False,
        "health": "OK",
        "tape_frozen": False,
        "runtime_basis": "requote",
        "paper_log": "livecfg/logs/paper.jsonl",
        "paper_log_files": 2,
        "quotes": 5600,
        "outage_alert": False,
        "outage_alert_severe": False,
        "outage_alert_prolonged": False,
        "outage_alert_critical": False,
        "outage_alert_imminent": False,
    }
    base.update(overrides)
    return base


def test_evaluate_recovery_pass() -> None:
    rep = evaluate_recovery(_recovered(), min_quotes=5529)
    assert rep["ok"] is True
    assert rep["blockers"] == []


def test_evaluate_recovery_accepts_status_ok() -> None:
    rep = evaluate_recovery(_recovered(recovered=False))
    assert rep["ok"] is True
    assert rep["checks"]["connectivity_up"] is True


def test_evaluate_recovery_fails_while_down() -> None:
    rep = evaluate_recovery(_recovered(
        connectivity="status=DOWN rest_ok=False ws_ok=False",
        recovered=False,
        outage_open=True,
        health="STALE",
        tape_frozen=True,
        outage_alert=True,
    ))
    assert rep["ok"] is False
    assert "connectivity_up" in rep["blockers"]
    assert "outage_closed" in rep["blockers"]
    assert "health_ok" in rep["blockers"]
    assert "tape_unfrozen" in rep["blockers"]


def test_evaluate_recovery_quotes_floor() -> None:
    rep = evaluate_recovery(_recovered(quotes=100), min_quotes=5529)
    assert rep["ok"] is False
    assert "quotes_floor" in rep["blockers"]


def test_evaluate_recovery_quotes_advanced(tmp_path: Path) -> None:
    snap = tmp_path / "frozen_tape_snapshot.json"
    snap.write_text(json.dumps({"quotes_at_freeze": 5529}) + "\n")
    stuck = evaluate_recovery(
        _recovered(quotes=5529, frozen_tape_snapshot=str(snap)),
        min_quotes=5529,
    )
    assert stuck["ok"] is False
    assert "quotes_advanced" in stuck["blockers"]
    assert stuck["quotes_at_freeze"] == 5529
    advanced = evaluate_recovery(
        _recovered(quotes=5600, frozen_tape_snapshot=str(snap)),
        min_quotes=5529,
    )
    assert advanced["ok"] is True
    assert advanced["checks"]["quotes_advanced"] is True


def test_recovery_smoke_cli(tmp_path: Path) -> None:
    path = tmp_path / "outage_status.json"
    path.write_text(json.dumps(_recovered()) + "\n")
    proc = subprocess.run(
        [sys.executable, "scripts/recovery_smoke.py", "--status", str(path), "--min-quotes", "5000"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "status=PASS" in proc.stderr

    path.write_text(json.dumps(_recovered(
        connectivity="status=DOWN",
        recovered=False,
        outage_open=True,
        health="STALE",
        tape_frozen=True,
        outage_alert=True,
    )) + "\n")
    proc2 = subprocess.run(
        [
            sys.executable,
            "scripts/recovery_smoke.py",
            "--status",
            str(path),
            "--allow-stale-health",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 1
    assert "status=FAIL" in proc2.stderr
