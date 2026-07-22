"""Tests for append_strategy_cycle outage_status embedding helpers."""

from __future__ import annotations

import json
from pathlib import Path

import scripts.append_strategy_cycle as append_mod


def test_load_outage_status_missing(tmp_path: Path) -> None:
    assert append_mod._load_outage_status(tmp_path / "nope.json") == {}


def test_load_outage_status_ok(tmp_path: Path) -> None:
    path = tmp_path / "outage_status.json"
    path.write_text(
        json.dumps({
            "hours_to_tier2_gate": 15.63,
            "tier2_allowed": False,
            "gate_reason": "need_hours>=24.0",
        })
        + "\n"
    )
    data = append_mod._load_outage_status(path)
    assert data["hours_to_tier2_gate"] == 15.63
    assert data["tier2_allowed"] is False
    assert data["gate_reason"] == "need_hours>=24.0"


def test_load_outage_status_invalid(tmp_path: Path) -> None:
    path = tmp_path / "outage_status.json"
    path.write_text("not-json\n")
    assert append_mod._load_outage_status(path) == {}
