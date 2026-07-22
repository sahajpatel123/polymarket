"""Tests for quote churn / lifetime report."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.metrics.churn import analyze_quote_churn


def test_quote_churn_lifetimes_and_intervals(tmp_path: Path) -> None:
    cid = "0xabc"
    t0 = 1_700_000_000.0
    rows = [
        {
            "ts": t0,
            "event": "quote",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.49,
            "order_id": "p1",
            "mid": 0.5,
        },
        {
            "ts": t0 + 10,
            "event": "quote",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.48,
            "order_id": "p2",
            "mid": 0.5,
        },
        {
            "ts": t0 + 25,
            "event": "cancel",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.48,
            "order_id": "p2",
        },
    ]
    path = tmp_path / "m.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze_quote_churn(path)
    assert rep.n_lifetimes == 2
    # p1 lived 10s (replaced), p2 lived 15s (cancelled)
    assert abs(rep.lifetime_mean_s - 12.5) < 1e-9
    assert rep.n_intervals == 1
    assert abs(rep.requote_interval_mean_s - 10.0) < 1e-9
    assert cid in rep.by_market
