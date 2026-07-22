"""Tests for paper regime/churn report (Tier-1 evidence for T2-04/T2-05)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.paper_regime_report import analyze_paper_log


def test_analyze_paper_log_counts_regimes_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    rows = [
        {"event": "requote", "condition_id": "0xa", "regime": "QUIET", "cancel": 0, "place": 2, "flowz": 0.1},
        {"event": "requote", "condition_id": "0xa", "regime": "TRENDING", "cancel": 1, "place": 1, "flowz": 0.0},
        {"event": "requote", "condition_id": "0xa", "regime": "QUIET", "cancel": 0, "place": 2, "flowz": -0.2},
        {"event": "other", "msg": "noise"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze_paper_log(path)
    assert rep["n_requote"] == 3
    assert rep["regimes"]["QUIET"] == 2
    assert rep["regimes"]["TRENDING"] == 1
    assert rep["regime_transitions"]["QUIET->TRENDING"] == 1
    assert rep["regime_transitions"]["TRENDING->QUIET"] == 1
    assert rep["cancel_sum"] == 1
    assert rep["place_sum"] == 5
    assert rep["trending_flowz_mean"] == 0.0
