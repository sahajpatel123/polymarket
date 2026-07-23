"""Tests for frozen_tape_snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.frozen_tape_snapshot import build_frozen_snapshot, write_frozen_snapshot


def test_build_frozen_snapshot_latches_quotes() -> None:
    status = {
        "ts": "2026-07-23T05:00:00+00:00",
        "tape_frozen": True,
        "outage_open": True,
        "quotes": 5529,
        "runtime_h": 8.37,
        "operator_mode": "CRITICAL_OPEN",
    }
    first = build_frozen_snapshot(status)
    assert first is not None
    assert first["quotes_at_freeze"] == 5529
    assert first["frozen_since"] == "2026-07-23T05:00:00+00:00"
    later = build_frozen_snapshot(
        {**status, "ts": "2026-07-23T06:00:00+00:00", "quotes": 5600},
        prev=first,
    )
    assert later is not None
    assert later["quotes"] == 5600
    assert later["quotes_at_freeze"] == 5529  # latched
    assert later["frozen_since"] == "2026-07-23T05:00:00+00:00"


def test_build_frozen_snapshot_clears_when_unfrozen() -> None:
    assert build_frozen_snapshot({"tape_frozen": False, "outage_open": False}) is None


def test_write_frozen_snapshot_roundtrip(tmp_path: Path) -> None:
    status_path = tmp_path / "outage_status.json"
    snap_path = tmp_path / "frozen_tape_snapshot.json"
    status_path.write_text(json.dumps({
        "ts": "2026-07-23T05:00:00+00:00",
        "tape_frozen": True,
        "outage_open": True,
        "quotes": 5529,
        "runtime_h": 8.37,
        "paper_log": "livecfg/logs/paper.jsonl.2026-07-22",
        "operator_mode": "CRITICAL_OPEN",
    }) + "\n")
    first = write_frozen_snapshot(status_path, snap_path)
    assert first["ok"] is True
    assert first["frozen"] is True
    assert first["quotes_at_freeze"] == 5529
    status_path.write_text(json.dumps({
        "ts": "2026-07-23T06:00:00+00:00",
        "tape_frozen": True,
        "outage_open": True,
        "quotes": 5529,
        "runtime_h": 8.37,
    }) + "\n")
    second = write_frozen_snapshot(status_path, snap_path)
    assert second["quotes_at_freeze"] == 5529
    assert second["frozen_since"] == "2026-07-23T05:00:00+00:00"
    status_path.write_text(json.dumps({
        "ts": "2026-07-23T07:00:00+00:00",
        "tape_frozen": False,
        "outage_open": False,
        "quotes": 5600,
    }) + "\n")
    cleared = write_frozen_snapshot(status_path, snap_path)
    assert cleared["frozen"] is False
    assert cleared["cleared"] is True
    assert cleared["quotes_at_freeze"] == 5529
