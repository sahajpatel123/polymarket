"""Tests for the latency benchmark harness (P1-01)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.bench_latency import (
    _default_meta,
    bench_pure_strategy,
    bench_replay,
    generate_synthetic_journal,
)

from polymaker.config import StrategyProfile


def test_default_meta_is_valid():
    meta = _default_meta()
    assert meta.tick_size == 0.01
    assert meta.yes.token_id != meta.no.token_id
    assert meta.condition_id.startswith("0x")


def test_generate_synthetic_journal(tmp_path: Path):
    journal = tmp_path / "bench.jsonl"
    generate_synthetic_journal(journal, n_events=100, seed=42)
    assert journal.exists()
    lines = journal.read_text().strip().split("\n")
    assert len(lines) >= 100
    # Each line is valid JSON with kind/ts/data
    for line in lines:
        obj = json.loads(line)
        assert "kind" in obj
        assert "ts" in obj
        assert "data" in obj


def test_bench_replay_returns_valid_metrics(tmp_path: Path):
    journal = tmp_path / "bench.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    generate_synthetic_journal(journal, n_events=500, seed=42)
    meta = _default_meta()
    profile = StrategyProfile()
    result = bench_replay(journal, meta, profile, metrics)
    assert result["events_total"] >= 500
    assert result["recomputes"] > 0
    assert result["latency_us"]["p50"] > 0
    assert result["latency_us"]["p95"] >= result["latency_us"]["p50"]
    assert result["latency_us"]["p99"] >= result["latency_us"]["p95"]
    assert result["events_per_s"] > 0
    assert metrics.exists()


def test_bench_pure_strategy_returns_valid_metrics(tmp_path: Path):
    meta = _default_meta()
    profile = StrategyProfile()
    result = bench_pure_strategy(meta, profile, n_iterations=1000)
    assert result["iterations"] == 1000
    assert result["ops_per_s"] > 0
    assert result["latency_us"]["p50"] > 0
    assert result["latency_us"]["p95"] >= result["latency_us"]["p50"]
    assert result["latency_us"]["p99"] >= result["latency_us"]["p95"]


def test_bench_replay_is_deterministic(tmp_path: Path):
    """Same journal + seed must produce identical latency profile."""
    journal = tmp_path / "bench.jsonl"
    metrics1 = tmp_path / "m1.jsonl"
    metrics2 = tmp_path / "m2.jsonl"
    generate_synthetic_journal(journal, n_events=500, seed=42)
    meta = _default_meta()
    profile = StrategyProfile()
    r1 = bench_replay(journal, meta, profile, metrics1)
    r2 = bench_replay(journal, meta, profile, metrics2)
    assert r1["recomputes"] == r2["recomputes"]
    assert r1["events_total"] == r2["events_total"]
