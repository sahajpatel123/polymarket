"""Tests for the PnL tracker script."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from scripts.pnl_tracker import compute_daily_pnl, load_events


def _write_metrics(tmp: Path, events: list[dict]) -> Path:
    path = tmp / "metrics.jsonl"
    with path.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return path


def test_load_events_empty() -> None:
    events = load_events(Path("/nonexistent/path.jsonl"))
    assert events == []


def test_load_events_skips_invalid() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "m.jsonl"
        path.write_text('{"event": "fill", "ts": 1.0}\ninvalid\n{"event": "mark", "ts": 2.0}\n')
        events = load_events(path)
        assert len(events) == 2


def test_compute_daily_pnl_spread() -> None:
    events = [
        {
            "event": "fill",
            "ts": 1700000000.0,  # 2023-11-14
            "price": 0.48,
            "size": 100,
            "side": "BUY",
            "mid": 0.50,
        },
    ]
    daily = compute_daily_pnl(events)
    day = "2023-11-14"
    assert day in daily
    # BUY below mid earns (mid - price) * size = 0.02 * 100 = 2.0
    assert abs(daily[day]["spread_pnl"] - 2.0) < 0.001
    assert daily[day]["fill_count"] == 1


def test_compute_daily_pnl_sell_side() -> None:
    events = [
        {
            "event": "fill",
            "ts": 1700000000.0,
            "price": 0.52,
            "size": 50,
            "side": "SELL",
            "mid": 0.50,
        },
    ]
    daily = compute_daily_pnl(events)
    # SELL above mid earns (price - mid) * size = 0.02 * 50 = 1.0
    assert abs(daily["2023-11-14"]["spread_pnl"] - 1.0) < 0.001


def test_compute_daily_pnl_reward() -> None:
    # Two quotes in-band 10s apart, daily_rate=100 -> 100 * 10/86400
    events = [
        {"event": "market_meta", "condition_id": "0xabc", "rewards_daily_rate": 100.0},
        {"event": "quote", "ts": 1700000000.0, "condition_id": "0xabc", "in_reward_band": True, "price": 0.5, "size": 10},
        {"event": "quote", "ts": 1700000010.0, "condition_id": "0xabc", "in_reward_band": True, "price": 0.5, "size": 10},
    ]
    daily = compute_daily_pnl(events)
    # 10 seconds in-band, rate=100 -> 100 * 10/86400 ≈ 0.01157
    assert daily["2023-11-14"]["reward_pnl"] > 0.01


def test_compute_daily_pnl_no_reward_when_no_in_band() -> None:
    events = [
        {"event": "market_meta", "condition_id": "0xabc", "rewards_daily_rate": 100.0},
        {"event": "quote", "ts": 1700000000.0, "condition_id": "0xabc", "in_reward_band": False, "price": 0.5, "size": 10},
        {"event": "quote", "ts": 1700000010.0, "condition_id": "0xabc", "in_reward_band": False, "price": 0.5, "size": 10},
    ]
    daily = compute_daily_pnl(events)
    assert daily["2023-11-14"]["reward_pnl"] == 0.0


def test_compute_daily_pnl_quote_cancel_counts() -> None:
    events = [
        {"event": "quote", "ts": 1700000000.0, "in_reward_band": False, "price": 0.5, "size": 10},
        {"event": "cancel", "ts": 1700000005.0, "price": 0.5, "size": 10},
        {"event": "mark", "ts": 1700000010.0, "fv": 0.5},
    ]
    daily = compute_daily_pnl(events)
    assert daily["2023-11-14"]["quote_count"] == 1
    assert daily["2023-11-14"]["cancel_count"] == 1
    assert daily["2023-11-14"]["mark_count"] == 1
