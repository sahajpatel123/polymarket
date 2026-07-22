"""Tests for paper_data_gate quote counting (BACKLOG: ≥500 quotes)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from polymaker.metrics.log_discovery import pick_richest_log


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


def test_paper_data_gate_runtime_uses_requote_not_outage_noise(tmp_path: Path) -> None:
    paper = tmp_path / "paper.jsonl"
    metrics = tmp_path / "metrics-paper.jsonl"
    t0 = 1_700_000_000.0
    rows = [
        {"ts": t0, "event": "requote", "regime": "QUIET"},
        {"ts": t0 + 3600, "event": "requote", "regime": "QUIET"},
        # Outage noise 5h after last requote must not pad runtime.
        {"ts": t0 + 6 * 3600, "event": "get_full_book_failed", "err": ""},
        {"ts": t0 + 7 * 3600, "event": "market_ws_dropped", "err": "timeout"},
    ]
    paper.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    metrics.write_text(
        json.dumps({"ts": t0, "event": "quote", "price": 0.4}) + "\n"
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
            "24",
            "--min-quotes",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    assert "runtime_basis=requote" in proc.stdout
    assert "runtime_hours=1.0000" in proc.stdout
    assert "runtime_hours_all_events=7.0000" in proc.stdout


def test_pick_richest_log_prefers_longer_runtime(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.jsonl"
    rich = tmp_path / "rich.jsonl"
    t0 = 1_700_000_000.0
    tiny.write_text(
        "\n".join(json.dumps({"ts": t0 + i, "event": "requote"}) for i in range(3)) + "\n"
    )
    rich.write_text(
        "\n".join(
            json.dumps({"ts": t0 + i * 3600, "event": "requote"}) for i in range(4)
        )
        + "\n"
    )
    picked = pick_richest_log([tiny, rich])
    assert picked == rich
