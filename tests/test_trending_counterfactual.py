"""Tests for offline TRENDING counterfactual (C-01)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.trending_counterfactual import analyze_counterfactual


def test_counterfactual_suppresses_vol_only(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    rows = [
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "TRENDING",
            "flowz": 0.1,
            "vol_ratio": 3.0,
            "cancel": 2,
            "place": 1,
        },
        {
            "event": "requote",
            "condition_id": "0xb",
            "regime": "TRENDING",
            "flowz": 2.5,
            "vol_ratio": 1.0,
            "cancel": 1,
            "place": 1,
        },
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "QUIET",
            "flowz": 0.0,
            "vol_ratio": 1.0,
            "cancel": 0,
            "place": 2,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze_counterfactual(path, trend_vol_ratio=8.0, trend_flow_z=1.2)
    assert rep["n_trending"] == 2
    assert rep["n_trending_with_vol"] == 2
    assert rep["would_suppress_n"] == 1  # vol-only row
    assert rep["would_suppress_frac"] == 0.5
    assert rep["suppress_cancel_sum"] == 2
    assert rep["keep_cancel_sum"] == 1


def test_counterfactual_sweep_by_market(tmp_path: Path) -> None:
    from scripts.trending_counterfactual import sweep_counterfactual

    path = tmp_path / "paper.jsonl"
    rows = [
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "TRENDING",
            "flowz": 0.0,
            "vol_ratio": 4.0,
            "cancel": 1,
            "place": 1,
        },
        {
            "event": "requote",
            "condition_id": "0xb",
            "regime": "TRENDING",
            "flowz": 0.0,
            "vol_ratio": 6.0,
            "cancel": 1,
            "place": 1,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = sweep_counterfactual(
        path, vol_values=[5.0, 8.0], trend_flow_z=1.2, by_market=True
    )
    assert len(rep["markets"]) == 2
    by_cid = {m["condition_id"]: m for m in rep["markets"]}
    # 0xa vol=4: suppressed at both 5 and 8
    assert by_cid["0xa"]["sweep"][0]["would_suppress_n"] == 1
    assert by_cid["0xa"]["sweep"][1]["would_suppress_n"] == 1
    # 0xb vol=6: kept at 5, suppressed at 8
    assert by_cid["0xb"]["sweep"][0]["would_suppress_n"] == 0
    assert by_cid["0xb"]["sweep"][1]["would_suppress_n"] == 1
