"""Smoke test for micro_levels EV sweep script helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_micro_levels_ev_sweep_fixture(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    cmd = [
        sys.executable,
        "scripts/micro_levels_ev_sweep.py",
        "--journal",
        "fixtures/regime_dense.jsonl",
        "--config-dir",
        "config",
        "--baseline-profile",
        "political-longdated",
        "--levels",
        "3,5",
        "--n-chunks",
        "3",
        "--holdout-frac",
        "0.3",
        "--tick-size",
        "0.01",
        "--report",
        str(report),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(report.read_text())
    assert data["baseline_micro_levels"] == 3
    assert len(data["rows"]) == 2
    assert {r["micro_levels"] for r in data["rows"]} == {3, 5}
