"""Tests for paper collector ensure/restart helper (diagnose path only)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def test_ensure_paper_collector_needs_restart_on_stale(tmp_path: Path) -> None:
    paper = tmp_path / "paper.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    old = time.time() - 1000
    paper.write_text(
        json.dumps({"event": "requote", "ts": old, "regime": "QUIET"}) + "\n"
    )
    metrics.write_text(
        json.dumps(
            {
                "ts": old,
                "event": "quote",
                "token_id": "yes",
                "side": "BUY",
                "price": 0.4,
                "order_id": "p0",
                "mid": 0.41,
                "fv_yes": 0.41,
            }
        )
        + "\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/ensure_paper_collector.py",
            "--paper-log",
            str(paper),
            "--metrics-log",
            str(metrics),
            "--max-age-s",
            "60",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "status=NEEDS_RESTART" in proc.stderr
