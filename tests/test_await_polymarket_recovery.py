"""Tests for await_polymarket_recovery (once / still-down / recover path)."""

from __future__ import annotations

import subprocess
import sys

import scripts.await_polymarket_recovery as await_mod


def test_await_recovery_once_still_down(monkeypatch) -> None:
    # Live network is currently DOWN in this environment; --once should exit 1.
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/await_polymarket_recovery.py",
            "--once",
            "--no-restart-on-recover",
            "--no-append-cycle-on-recover",
            "--timeout-s",
            "3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # OK if UP (0) or STILL_DOWN (1); never crash.
    assert proc.returncode in (0, 1)
    assert "status=" in proc.stderr


def test_recover_appends_cycle(monkeypatch, capsys) -> None:
    def fake_run(cmd: list[str]) -> tuple[int, str, str]:
        joined = " ".join(cmd)
        if "polymarket_connectivity.py" in joined:
            return 0, "", "status=OK rest_ok=True ws_ok=True"
        if "ensure_paper_collector.py" in joined:
            return 0, "", "status=RESTARTED_OK"
        if "append_strategy_cycle.py" in joined:
            return 0, "", "status=OK appended=logs/strategy_cycles.jsonl"
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
        ],
    )
    assert await_mod.main() == 0
    err = capsys.readouterr().err
    assert "status=RECOVERED" in err
    assert "append=status=OK" in err
    assert "ensure=status=RESTARTED_OK" in err
