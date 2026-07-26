"""Smoke test for band_touch_tradeoff script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_band_touch_tradeoff_runs(tmp_path: Path) -> None:
    db = Path("livecfg/state.db")
    journal = Path("livecfg/journal/paper.jsonl.pre12h.1784925687.31229")
    if not db.exists() or not journal.exists():
        pytest.skip("livecfg db/journal missing")
    report = tmp_path / "report.json"
    cmd = [
        sys.executable,
        "scripts/band_touch_tradeoff.py",
        "--journal",
        str(journal),
        "--slug",
        "will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568",
        "--db",
        str(db),
        "--config-dir",
        "livecfg",
        "--spreads",
        "5.5,1.0",
        "--report",
        str(report),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(report.read_text())
    assert len(data["rows"]) == 2
    assert "any_crossable" in data
