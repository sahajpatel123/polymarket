"""Tests for deterministic journal replay harness (T1-02)."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.metrics.analyze import analyze
from polymaker.replay import run_replay


def _journal_fixture(path: Path, yes: str = "yes-token", no: str = "no-token",
                     market: str = "0xreplay") -> None:
    """Minimal journal: snapshots + a trade + a price_change."""
    t0 = 1_700_000_000.0
    rows = [
        {
            "ts": t0,
            "kind": "book",
            "data": {
                "market": market,
                "asset_id": yes,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int(t0 * 1000)),
                "tick_size": "0.01",
            },
        },
        {
            "ts": t0 + 0.1,
            "kind": "book",
            "data": {
                "market": market,
                "asset_id": no,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int((t0 + 0.1) * 1000)),
                "tick_size": "0.01",
            },
        },
        {
            "ts": t0 + 1.0,
            "kind": "last_trade_price",
            "data": {
                "market": market,
                "asset_id": yes,
                "price": "0.50",
                "size": "25",
                "side": "BUY",
                "timestamp": str(int((t0 + 1.0) * 1000)),
            },
        },
        {
            "ts": t0 + 2.0,
            "kind": "price_change",
            "data": {
                "market": market,
                "timestamp": str(int((t0 + 2.0) * 1000)),
                "price_changes": [
                    {"asset_id": yes, "price": "0.49", "size": "400", "side": "BUY"},
                    {"asset_id": yes, "price": "0.51", "size": "450", "side": "SELL"},
                ],
            },
        },
        {
            "ts": t0 + 3.0,
            "kind": "book",
            "data": {
                "market": market,
                "asset_id": yes,
                "bids": [{"price": "0.47", "size": "300"}, {"price": "0.48", "size": "300"}],
                "asks": [{"price": "0.52", "size": "300"}, {"price": "0.53", "size": "300"}],
                "timestamp": str(int((t0 + 3.0) * 1000)),
                "tick_size": "0.01",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_replay_emits_metrics_analyzable_by_t1_script(tmp_path: Path, meta) -> None:
    journal = tmp_path / "paper.jsonl"
    metrics = tmp_path / "metrics-replay.jsonl"
    _journal_fixture(journal, yes=meta.yes.token_id, no=meta.no.token_id,
                     market=meta.condition_id)

    result = run_replay(journal, meta, StrategyProfile(), metrics)
    assert result.events_read == 5
    assert result.events_applied >= 3
    assert result.recomputes >= 1
    assert result.n_quote >= 1
    assert result.n_mark >= 1
    assert metrics.exists()

    rep = analyze(metrics)
    assert rep.n_quote == result.n_quote
    assert rep.n_mark >= 1
    assert meta.condition_id in rep.markets
    # schema contract with T1-01
    d = rep.as_dict()
    for key in (
        "realized_spread_usdc",
        "markout_mean",
        "inventory_drift_abs_peak",
        "reward_accrual_usdc",
    ):
        assert key in d


def test_replay_is_deterministic(tmp_path: Path, meta) -> None:
    journal = tmp_path / "j.jsonl"
    _journal_fixture(journal, yes=meta.yes.token_id, no=meta.no.token_id,
                     market=meta.condition_id)
    m1 = tmp_path / "m1.jsonl"
    m2 = tmp_path / "m2.jsonl"
    r1 = run_replay(journal, meta, StrategyProfile(), m1)
    r2 = run_replay(journal, meta, StrategyProfile(), m2)
    assert r1.n_quote == r2.n_quote
    assert r1.n_mark == r2.n_mark
    # strip timestamps? quotes should match on price/size/side
    def quote_keys(path: Path) -> list[tuple]:
        out = []
        for line in path.read_text().splitlines():
            e = json.loads(line)
            if e.get("event") == "quote":
                out.append((e["token_id"], e["side"], e["price"], e["size"]))
        return out

    assert quote_keys(m1) == quote_keys(m2)
