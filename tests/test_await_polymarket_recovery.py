"""Tests for await_polymarket_recovery (once / still-down / recover path)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import scripts.await_polymarket_recovery as await_mod


def test_await_recovery_once_still_down(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    status_path = tmp_path / "outage_status.json"
    calls: list[str] = []

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        joined = " ".join(cmd)
        calls.append(joined)
        if "polymarket_connectivity.py" in joined:
            return 1, "", "status=DOWN rest_ok=False ws_ok=False"
        if "outage_window_report.py" in joined:
            status_path.write_text(
                json.dumps({
                    "outage_open": True,
                    "outage_alert": True,
                    "outage_alert_severe": True,
                    "duration_h": 5.5,
                })
                + "\n"
            )
            return 0, "", "status=OK open=True duration_h=5.5"
        return 1, "", "status=UNKNOWN"

    monkeypatch.setattr(await_mod, "_run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "await_polymarket_recovery.py",
            "--once",
            "--no-restart-on-recover",
            "--no-append-cycle-on-recover",
            "--status-out",
            str(status_path),
            "--timeout-s",
            "3",
        ],
    )
    assert await_mod.main() == 1
    err = capsys.readouterr().err
    assert "status=STILL_DOWN" in err
    assert any("outage_window_report.py" in c for c in calls)
    data = json.loads(status_path.read_text())
    assert data["outage_open"] is True
    assert data["outage_alert_severe"] is True


def test_await_recovery_once_still_down_live() -> None:
    # Live network may be UP or DOWN; --once should never crash.
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/await_polymarket_recovery.py",
            "--once",
            "--no-restart-on-recover",
            "--no-append-cycle-on-recover",
            "--status-out",
            "",
            "--timeout-s",
            "3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode in (0, 1)
    assert "status=" in proc.stderr


def test_recover_appends_cycle(tmp_path: Path, monkeypatch, capsys) -> None:
    status_path = tmp_path / "outage_status.json"
    calls: list[str] = []

    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        joined = " ".join(cmd)
        calls.append(joined)
        if "polymarket_connectivity.py" in joined:
            return 0, "", "status=OK rest_ok=True ws_ok=True"
        if "ensure_paper_collector.py" in joined:
            return 0, "", "status=RESTARTED_OK"
        if "append_strategy_cycle.py" in joined:
            return 0, "", "status=OK appended=logs/strategy_cycles.jsonl"
        if "outage_window_report.py" in joined:
            status_path.write_text(
                json.dumps({"outage_open": True, "duration_h": 5.5}) + "\n"
            )
            return 0, "", "status=OK open=True duration_h=5.5"
        return 1, "", "status=UNKNOWN"

    monkeypatch.setattr(await_mod, "_run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "await_polymarket_recovery.py",
            "--once",
            "--wait-s",
            "0",
            "--status-out",
            str(status_path),
        ],
    )
    assert await_mod.main() == 0
    err = capsys.readouterr().err
    assert "status=RECOVERED" in err
    assert "append=status=OK" in err
    assert "ensure=status=RESTARTED_OK" in err
    assert any("outage_window_report.py" in c for c in calls)
    data = json.loads(status_path.read_text())
    assert data["recovered"] is True
    assert data["outage_open"] is False
    assert data["connectivity"] == "status=OK rest_ok=True ws_ok=True"
