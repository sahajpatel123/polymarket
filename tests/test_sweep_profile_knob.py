"""Tests for offline StrategyProfile knob sweep harness."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.synth import write_regime_journal


def test_sweep_profile_knob_cli(tmp_path: Path) -> None:
    import subprocess
    import sys

    journal = tmp_path / "j.jsonl"
    write_regime_journal(journal)
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/sweep_profile_knob.py",
            "--journal",
            str(journal),
            "--baseline-profile",
            "newsom-mm",
            "--knob",
            "trend_vol_ratio",
            "--values",
            "2,5",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["knob"] == "trend_vol_ratio"
    assert len(payload["rows"]) == 2
    assert "status=OK" in proc.stderr
