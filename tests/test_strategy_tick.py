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
        "log_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-22\n"
        "log_files=2\n"
        "metrics_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl\n"
    )
    assert fields["tier2_allowed"] is False
    assert fields["gate_reason"] == "need_hours>=24.0"
    assert fields["runtime_basis"] == "requote"
    assert fields["gate_runtime_h"] == 8.37
    assert fields["gate_quotes"] == 5529
    assert fields["paper_log"].endswith("paper.jsonl.2026-07-22")
    assert fields["paper_log_files"] == 2
    assert fields["metrics_log"].endswith("metrics-paper.jsonl")


def test_summarize_freeze_fields() -> None:
    fields = tick._summarize_freeze_fields({
        "tape_frozen": "True",
        "eta_paused": "True",
        "last_requote_age_s": "25225.3",
        "runtime_h": "8.37",
        "cycles": "79",
    })
    assert fields == {
        "tape_frozen": True,
        "eta_paused": True,
        "last_requote_age_s": 25225.3,
        "n_cycles": 79,
    }


def test_live_health_fields_overwrite_stale_age() -> None:
    fields = tick._live_health_fields({
        "status": "STALE",
        "last_requote_age_s": "25845.5",
        "last_quote_age_s": "25840.1",
    })
    assert fields["health"] == "STALE"
    assert fields["tape_frozen"] is True
    assert fields["last_requote_age_s"] == 25845.5
    assert fields["last_quote_age_s"] == 25840.1


def test_ensure_collector_fields() -> None:
    fields = tick._ensure_collector_fields({
        "status": "NEEDS_RESTART",
        "pids": "[78216]",
    })
    assert fields["ensure_status"] == "NEEDS_RESTART"
    assert fields["collector_pids"] == "[78216]"
    assert fields["collector_pid"] == 78216


def test_c01_blocker_fields() -> None:
    fields = tick._c01_blocker_fields({
        "status": "BLOCKED",
        "blockers": "hours_ok,health_ok,outage_closed",
    })
    assert fields == {
        "c01_status": "BLOCKED",
        "c01_blockers": "hours_ok,health_ok,outage_closed",
    }
