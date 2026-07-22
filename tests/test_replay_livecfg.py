"""Tests for livecfg journal token inference + replay helper."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay import discover_condition_ids, infer_yes_no_tokens


def test_infer_yes_no_tokens_picks_lower_price_as_yes(tmp_path: Path) -> None:
    metrics = tmp_path / "m.jsonl"
    cid = "0xabc"
    rows = [
        {"event": "quote", "condition_id": cid, "token_id": "yes1", "price": 0.22, "side": "BUY"},
        {"event": "quote", "condition_id": cid, "token_id": "yes1", "price": 0.21, "side": "BUY"},
        {"event": "quote", "condition_id": cid, "token_id": "no1", "price": 0.78, "side": "BUY"},
        {"event": "quote", "condition_id": cid, "token_id": "no1", "price": 0.79, "side": "BUY"},
        {"event": "quote", "condition_id": "0xother", "token_id": "x", "price": 0.1, "side": "BUY"},
    ]
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    pair = infer_yes_no_tokens(metrics, cid)
    assert pair == ("yes1", "no1")
    assert discover_condition_ids(metrics) == ["0xabc", "0xother"]
