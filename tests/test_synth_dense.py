"""Tests for dense multi-cycle regime synth journals."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from polymaker.replay.synth import generate_regime_journal, write_regime_journal


def test_generate_regime_journal_cycles_scales_events() -> None:
    one = generate_regime_journal(cycles=1, quiet_steps=8, recovery_steps=6)
    four = generate_regime_journal(cycles=4, quiet_steps=8, recovery_steps=6)
    assert len(four) == 4 * len(one)


def test_dense_synth_holdout_not_thin(tmp_path: Path) -> None:
    """Dense tape should clear the validator's thin_holdout (<20 quotes) flag."""
    journal = tmp_path / "dense.jsonl"
    write_regime_journal(
        journal, quiet_steps=20, recovery_steps=12, cycles=8, jump_ticks=10
    )
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
            "2,5,8",
            "--holdout-frac",
            "0.3",
            "--split",
            "events",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["thin_holdout"] is False
    hold_n = int((payload.get("best_on_holdout") or {}).get("baseline_n_quote") or 0)
    assert hold_n >= 20
    assert "status=OK" in proc.stderr
