"""Tests for scanner-rank vs realized-reward report."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.rank_vs_realized import build_report, spearman_rank_corr


def test_spearman_perfect() -> None:
    assert spearman_rank_corr([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == 1.0


def test_spearman_inverse() -> None:
    assert spearman_rank_corr([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == -1.0


def test_build_report_flags_disagreement(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE markets (condition_id TEXT, question TEXT, slug TEXT, "
        "meta_json TEXT NOT NULL, score REAL, score_json TEXT, scanned_ts REAL)"
    )
    con.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?)",
        ("0xa", "A", "a", "{}", 0.9, json.dumps({"reward_density": 1.0}), 0.0),
    )
    con.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?)",
        ("0xb", "B", "b", "{}", 0.1, json.dumps({"reward_density": 0.1}), 0.0),
    )
    con.commit()
    con.close()

    metrics = tmp_path / "metrics-paper.jsonl"
    t0 = 1_700_000_000.0
    rows = [
        {"ts": t0, "event": "market_meta", "condition_id": "0xa", "rewards_daily_rate": 10, "rebate_rate": 0.25},
        {"ts": t0, "event": "market_meta", "condition_id": "0xb", "rewards_daily_rate": 50, "rebate_rate": 0.25},
        {"ts": t0 + 1, "event": "quote", "condition_id": "0xa", "token_id": "y", "side": "BUY", "price": 0.4, "size": 10, "in_reward_band": True},
        {"ts": t0 + 3600, "event": "quote", "condition_id": "0xa", "token_id": "y", "side": "BUY", "price": 0.4, "size": 10, "in_reward_band": True},
        {"ts": t0 + 1, "event": "quote", "condition_id": "0xb", "token_id": "y", "side": "BUY", "price": 0.5, "size": 10, "in_reward_band": True},
        {"ts": t0 + 3600, "event": "quote", "condition_id": "0xb", "token_id": "y", "side": "BUY", "price": 0.5, "size": 10, "in_reward_band": True},
        {"ts": t0 + 3600, "event": "mark", "condition_id": "0xa", "fv": 0.4, "net": 0},
        {"ts": t0 + 3600, "event": "mark", "condition_id": "0xb", "fv": 0.5, "net": 0},
    ]
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = build_report(db=db, metrics=metrics, paper_log=None)
    assert rep["n_markets"] == 2
    assert rep["spearman_scanner_vs_realized"] == -1.0
    assert len(rep["rank_disagreements"]) == 2
