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
            "regime": "TRENDING",
            "flowz": 0.1,
            "vol_ratio": 3.0,
            "cancel": 2,
            "place": 1,
        },
        {
            "event": "requote",
            "regime": "TRENDING",
            "flowz": 2.5,
            "vol_ratio": 1.0,
            "cancel": 1,
            "place": 1,
        },
        {
            "event": "requote",
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
