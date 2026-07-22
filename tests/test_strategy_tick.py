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


def test_parse_gate_stdout() -> None:
    fields = tick._parse_gate_stdout(
        "status=OK\nruntime_basis=requote\nruntime_hours=8.3700\n"
        "quotes_for_gate=5529\ntier2_allowed=false reason=need_hours>=24.0\n"
    )
    assert fields["tier2_allowed"] is False
    assert fields["gate_reason"] == "need_hours>=24.0"
    assert fields["runtime_basis"] == "requote"
    assert fields["gate_runtime_h"] == 8.37
    assert fields["gate_quotes"] == 5529


def test_summarize_freeze_fields() -> None:
    fields = tick._summarize_freeze_fields({
        "tape_frozen": "True",
        "eta_paused": "True",
        "last_requote_age_s": "25225.3",
        "runtime_h": "8.37",
    })
    assert fields == {
        "tape_frozen": True,
        "eta_paused": True,
        "last_requote_age_s": 25225.3,
    }
