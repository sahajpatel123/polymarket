"""Tests for paper regime/churn report (Tier-1 evidence for T2-04/T2-05)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.paper_regime_report import analyze_paper_log


def test_analyze_paper_log_counts_regimes_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    rows = [
        {"event": "requote", "condition_id": "0xa", "regime": "QUIET", "cancel": 0, "place": 2, "flowz": 0.1},
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "TRENDING",
            "cancel": 1,
            "place": 1,
            "flowz": 0.0,
            "vol_ratio": 3.0,
        },
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "TRENDING",
            "cancel": 3,
            "place": 3,
            "flowz": 2.5,
            "vol_ratio": 1.1,
        },
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "TRENDING",
            "cancel": 0,
            "place": 1,
            "flowz": 2.0,
            "vol_ratio": 4.0,
        },
        {"event": "requote", "condition_id": "0xa", "regime": "QUIET", "cancel": 0, "place": 2, "flowz": -0.2},
        {"event": "other", "msg": "noise"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze_paper_log(path)
    assert rep["n_requote"] == 5
    assert rep["regimes"]["QUIET"] == 2
    assert rep["regimes"]["TRENDING"] == 3
    assert rep["regime_transitions"]["QUIET->TRENDING"] == 1
    assert rep["regime_transitions"]["TRENDING->QUIET"] == 1
    assert rep["cancel_sum"] == 4
    assert rep["place_sum"] == 9
    assert rep["false_trending_n"] == 1
    assert rep["false_trending_frac"] == 0.333333
    assert rep["false_trending_attributed_n"] == 1
    assert rep["false_trending_attributed_frac"] == 0.333333
    assert rep["false_trending_cancel_sum"] == 1
    assert abs(rep["false_trending_cancel_share"] - 0.25) < 1e-9
    assert rep["trending_path"] == {"vol_only": 1, "flow_only": 1, "both": 1}
    assert rep["trending_vol_only_frac"] == 0.333333
    assert rep["trending_vol_ratio_mean"] == round((3.0 + 1.1 + 4.0) / 3, 6)
    assert rep["trending_vol_ratio"]["n"] == 3
    assert rep["trending_vol_ratio"]["min"] == 1.1
    assert rep["trending_vol_ratio"]["max"] == 4.0
    # QUIET rows in fixture lack vol_ratio
    assert rep["quiet_vol_ratio"]["n"] == 0


def test_vol_ratio_quiet_trend_gap(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    rows = [
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "QUIET",
            "cancel": 0,
            "place": 1,
            "flowz": 0.0,
            "vol_ratio": 1.5,
        },
        {
            "event": "requote",
            "condition_id": "0xa",
            "regime": "TRENDING",
            "cancel": 1,
            "place": 1,
            "flowz": 0.0,
            "vol_ratio": 2.5,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze_paper_log(path)
    assert rep["quiet_vol_ratio"]["max"] == 1.5
    assert rep["trending_vol_ratio"]["min"] == 2.5
    assert rep["vol_ratio_quiet_trend_gap"] == 1.0
    assert rep["suggested_trend_vol_ratio"] == 2.0
    assert rep["false_trending_attributed_n"] == 1
    assert rep["false_trending_attributed_frac"] == 1.0


def test_trending_path_missing_vol_legacy(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "requote",
                "condition_id": "0xa",
                "regime": "TRENDING",
                "cancel": 1,
                "place": 1,
                "flowz": 0.0,
            }
        )
        + "\n"
    )
    rep = analyze_paper_log(path)
    assert rep["trending_path"] == {"missing_vol": 1}
    assert rep["trending_vol_only_frac"] is None
    assert rep["false_trending_n"] == 1
    assert rep["false_trending_attributed_n"] == 0
    assert rep["false_trending_attributed_frac"] is None
    assert rep["suggested_trend_vol_ratio"] is None
