"""Tests for full+holdout knob validation helper."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from polymaker.replay.synth import write_regime_journal


def test_validate_knob_candidate_cli(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    write_regime_journal(journal, quiet_steps=12, recovery_steps=8)
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/validate_knob_candidate.py",
            "--journal",
            str(journal),
            "--baseline-profile",
            "newsom-mm",
            "--knob",
            "reprice_ticks",
            "--values",
            "1,2,5",
            "--holdout-frac",
            "0.3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "full" in payload and "holdout" in payload
    assert "oos_replicated" in payload
    assert "status=OK" in proc.stderr


def test_validate_knob_candidate_also_set(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    write_regime_journal(journal, quiet_steps=12, recovery_steps=8)
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/validate_knob_candidate.py",
            "--journal",
            str(journal),
            "--baseline-profile",
            "newsom-mm",
            "--knob",
            "trend_vol_ratio",
            "--values",
            "8",
            "--also-set",
            "trend_flow_z=2.0",
            "--holdout-frac",
            "0.3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["also_set"] == {"trend_flow_z": 2.0}
    assert "also_set=trend_flow_z=2.0" in proc.stderr
    assert "holdout_base_nq=" in proc.stderr
