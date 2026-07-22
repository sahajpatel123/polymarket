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


def test_parse_status_line_vol_context() -> None:
    kv = chk._parse_status_line(
        "status=OK quiet_vol_max=1.99 trend_vol_min=2.03 vol_gap=0.04 "
        "quiet_vol_p90=1.22 trend_vol_p50=3.11"
    )
    assert kv["quiet_vol_max"] == "1.99"
    assert kv["trend_vol_min"] == "2.03"
    assert kv["vol_gap"] == "0.04"
