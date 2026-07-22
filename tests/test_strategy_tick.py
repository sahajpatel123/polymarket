"""Tests for strategy_tick status parsing helpers."""

from __future__ import annotations

import scripts.strategy_tick as tick


def test_parse_kv_extracts_c01_fields() -> None:
    kv = tick._parse_kv(
        "status=BLOCKED blockers=hours_ok,health_ok outage_alert=True tape_frozen=True"
    )
    assert kv["status"] == "BLOCKED"
    assert kv["blockers"] == "hours_ok,health_ok"
    assert kv["outage_alert"] == "True"


def test_status_line_prefers_stderr() -> None:
    line = tick._status_line("status=OK runtime_h=8.37\n", "{}\n")
    assert line.startswith("status=OK")
