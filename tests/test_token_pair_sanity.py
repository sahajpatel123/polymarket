"""Tests for token-pair sanity."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.token_pair_sanity import assess_token_pair


def _book(asset: str, bid: float, ask: float, ts: float) -> dict:
    return {
        "kind": "book",
        "ts": ts,
        "data": {
            "asset_id": asset,
            "bids": [{"price": str(bid), "size": "10"}],
            "asks": [{"price": str(ask), "size": "10"}],
            "timestamp": str(int(ts * 1000)),
        },
    }


def test_token_pair_ok(tmp_path: Path) -> None:
    j = tmp_path / "ok.jsonl"
    rows = []
    for i in range(5):
        rows.append(_book("yes-token", 0.40, 0.41, 1000 + i))
        rows.append(_book("no-token", 0.59, 0.60, 1000 + i + 0.1))
    j.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    r = assess_token_pair(j, "yes-token", "no-token", tol=0.05)
    assert r.pair_ok is True
    assert r.mean_sum is not None and abs(r.mean_sum - 1.0) < 0.05


def test_token_pair_bad(tmp_path: Path) -> None:
    j = tmp_path / "bad.jsonl"
    rows = []
    for i in range(5):
        rows.append(_book("yes-token", 0.58, 0.59, 1000 + i))
        rows.append(_book("no-token", 0.19, 0.20, 1000 + i + 0.1))
    j.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    r = assess_token_pair(j, "yes-token", "no-token", tol=0.02)
    assert r.pair_ok is False
    assert r.mean_sum is not None and r.mean_sum < 0.9
