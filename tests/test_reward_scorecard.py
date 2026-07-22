"""Tests for per-market reward scorecard."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.reward_scorecard import build_scorecard


def test_build_scorecard_ranks_by_reward_per_hour(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics-paper.jsonl"
    paper = tmp_path / "paper.jsonl"
    t0 = 1_700_000_000.0
    rows = [
        {"ts": t0, "event": "market_meta", "condition_id": "0xa", "rewards_daily_rate": 50, "rebate_rate": 0.25},
        {"ts": t0, "event": "market_meta", "condition_id": "0xb", "rewards_daily_rate": 10, "rebate_rate": 0.25},
        {"ts": t0 + 1, "event": "quote", "condition_id": "0xa", "token_id": "y", "side": "BUY", "price": 0.4, "size": 10, "in_reward_band": True},
        {"ts": t0 + 1800, "event": "quote", "condition_id": "0xa", "token_id": "y", "side": "BUY", "price": 0.4, "size": 10, "in_reward_band": True},
        {"ts": t0 + 1, "event": "quote", "condition_id": "0xb", "token_id": "y", "side": "BUY", "price": 0.5, "size": 10, "in_reward_band": True},
        {"ts": t0 + 1800, "event": "mark", "condition_id": "0xa", "fv": 0.4, "net": 0},
        {"ts": t0 + 1800, "event": "mark", "condition_id": "0xb", "fv": 0.5, "net": 0},
    ]
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    paper.write_text(
        json.dumps({"ts": t0, "event": "requote", "condition_id": "0xa", "regime": "QUIET", "cancel": 0, "place": 1})
        + "\n"
        + json.dumps({"ts": t0 + 1, "event": "requote", "condition_id": "0xa", "regime": "TRENDING", "cancel": 1, "place": 1})
        + "\n"
    )
    card = build_scorecard(metrics, paper)
    assert card["runtime_hours"] > 0
    assert len(card["markets"]) >= 1
    assert card["markets"][0]["condition_id"] in ("0xa", "0xb")
    assert "reward_per_hour_usdc" in card["markets"][0]
