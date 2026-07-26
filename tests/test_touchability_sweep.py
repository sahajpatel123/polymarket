"""Smoke test for touchability sweep script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_touchability_sweep_fixture(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    cmd = [
        sys.executable,
        "scripts/touchability_sweep.py",
        "--journal",
        "fixtures/regime_dense.jsonl",
        "--config-dir",
        "config",
        "--baseline-profile",
        "political-longdated",
        "--delta-min-ticks",
        "0,1",
        "--c-vol",
        "1.5",
        "--min-edge-ticks",
        "1",
        "--tick-size",
        "0.01",
        "--report",
        str(report),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(report.read_text())
    assert "rows" in data and len(data["rows"]) == 2
    assert "any_crossable" in data
