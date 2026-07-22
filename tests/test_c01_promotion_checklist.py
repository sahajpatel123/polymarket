"""Tests for C-01 promotion checklist status parsing helpers."""

from __future__ import annotations

import scripts.c01_promotion_checklist as chk


def test_parse_gate_stdout_runtime_basis() -> None:
    text = "\n".join(
        [
            "status=OK lines=10 json_lines=10 bad_lines=0",
            "runtime_basis=requote",
            "runtime_hours=8.3700",
            "quotes_for_gate=5529",
            "tier2_allowed=false reason=need_hours>=24.0",
        ]
    )
    kv = chk._parse_gate_stdout(text)
    assert kv["runtime_basis"] == "requote"
    assert kv["runtime_hours"] == "8.3700"
    assert kv["quotes_for_gate"] == "5529"


def test_parse_status_line_outage() -> None:
    kv = chk._parse_status_line(
        "status=OK windows=1 open=True total_h=1.3 current_duration_s=100"
    )
    assert kv["status"] == "OK"
    assert kv["open"] == "True"


def test_parse_status_line_c01_blockers() -> None:
    kv = chk._parse_status_line(
        "status=BLOCKED blockers=hours_ok,health_ok vol_gap=0.04 suggested_vol=2.489 "
        "outage_alert=True outage_alert_severe=True last_requote_age_s=12000"
    )
    assert kv["status"] == "BLOCKED"
    assert kv["blockers"] == "hours_ok,health_ok"
    assert kv["suggested_vol"] == "2.489"
    assert kv["outage_alert"] == "True"
    assert kv["outage_alert_severe"] == "True"
    assert kv["last_requote_age_s"] == "12000"
