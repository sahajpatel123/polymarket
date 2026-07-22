"""Tests for await_polymarket_recovery (once / still-down path)."""

from __future__ import annotations

import subprocess
import sys


def test_await_recovery_once_still_down(monkeypatch) -> None:
    # Live network is currently DOWN in this environment; --once should exit 1.
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/await_polymarket_recovery.py",
            "--once",
            "--no-restart-on-recover",
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
