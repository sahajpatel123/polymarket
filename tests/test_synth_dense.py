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
    """Dense multi-cycle tape is large enough for event-holdout validation.

    Sticky requote logic may emit few place events on flat quiet segments; the
    harness still needs a long event window. Assert event density + that the
    validator completes with a non-empty holdout slice (not crash/empty).
    """
    dense = tmp_path / "dense.jsonl"
    jump = tmp_path / "jump.jsonl"
    write_regime_journal(
        dense, quiet_steps=20, recovery_steps=12, cycles=8, jump_ticks=10
    )
    write_regime_journal(
        jump, quiet_steps=8, recovery_steps=6, cycles=1, jump_ticks=10
    )
    dense_n = sum(1 for _ in dense.open())
    jump_n = sum(1 for _ in jump.open())
    assert dense_n >= 5 * jump_n
    assert dense_n >= 500

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/validate_knob_candidate.py",
            "--journal",
            str(dense),
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
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(proc.stdout)
    hold = payload.get("holdout") or {}
    window = hold.get("window") or {}
    assert int(window.get("n_events") or 0) >= 100
    assert "status=OK" in proc.stderr
