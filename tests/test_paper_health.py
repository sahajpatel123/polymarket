"""Tests for paper collector staleness watchdog."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def test_paper_health_ok_on_fresh_logs(tmp_path: Path) -> None:
    now = time.time()
    paper = tmp_path / "paper.jsonl"
    metrics = tmp_path / "metrics-paper.jsonl"
    paper.write_text(json.dumps({"event": "requote", "ts": now, "regime": "QUIET"}) + "\n")
    metrics.write_text(json.dumps({"event": "quote", "ts": now, "price": 0.4}) + "\n")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/paper_health.py",
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
    assert proc.returncode == 0, proc.stderr
    assert "status=OK" in proc.stderr


def test_paper_health_stale_when_old(tmp_path: Path) -> None:
    old = time.time() - 10_000
    paper = tmp_path / "paper.jsonl"
    metrics = tmp_path / "metrics-paper.jsonl"
    paper.write_text(json.dumps({"event": "requote", "ts": old, "regime": "QUIET"}) + "\n")
    metrics.write_text(json.dumps({"event": "quote", "ts": old, "price": 0.4}) + "\n")
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/paper_health.py",
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
    assert "status=STALE" in proc.stderr
