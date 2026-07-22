"""Tests for weekly report writer helpers."""

from __future__ import annotations

import scripts.write_weekly_report as wr


def test_status_line_picks_first() -> None:
    assert wr._status_line("noise\nstatus=OK a=1\n", "").startswith("status=OK")


def test_gate_block_filters_noise() -> None:
    text = "\n".join(
        [
            "paper_data_gate now=…",
            "status=OK",
            "runtime_hours=8.37",
            '{"ignored": true}',
        ]
    )
    block = wr._gate_block(text)
    assert "status=OK" in block
    assert "runtime_hours=8.37" in block
    assert "paper_data_gate" not in block
    assert "ignored" not in block
