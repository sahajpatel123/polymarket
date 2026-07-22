"""Tests for paper_data_gate quote counting (BACKLOG: ≥500 quotes)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_paper_data_gate_prefers_metrics_quotes(tmp_path: Path) -> None:
    paper = tmp_path / "paper.jsonl"
    metrics = tmp_path / "metrics-paper.jsonl"
    t0 = 1_700_000_000.0
    paper.write_text(
        "\n".join(
            json.dumps({"ts": t0 + i, "event": "requote", "regime": "QUIET"})
            for i in range(3)
        )
        + "\n"
    )
    metrics.write_text(
        "\n".join(json.dumps({"ts": t0 + i, "event": "quote", "price": 0.4}) for i in range(7))
        + "\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/paper_data_gate.py",
            "--log",
            str(paper),
            "--metrics-log",
            str(metrics),
            "--min-hours",
            "0",
            "--min-quotes",
            "5",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "quote_events=7" in out
    assert "requote_lines=3" in out
    assert "quotes_for_gate=7" in out
    assert "tier2_allowed=true" in out
