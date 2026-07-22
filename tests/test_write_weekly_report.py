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


def test_outage_status_block(tmp_path) -> None:
    missing = wr._outage_status_block(tmp_path / "missing.json")
    assert "missing" in missing
    path = tmp_path / "outage_status.json"
    path.write_text(
        '{"connectivity":"status=DOWN","outage_total_h":6.1,'
        '"hours_to_tier2_gate":15.63,"tier2_allowed":false,'
        '"gate_reason":"need_hours>=24.0"}\n'
    )
    block = wr._outage_status_block(path)
    assert "connectivity=status=DOWN" in block
    assert "hours_to_tier2_gate=15.63" in block
    assert "tier2_allowed=False" in block
    assert "gate_reason=need_hours>=24.0" in block


def test_count_changelog_tier1(tmp_path) -> None:
    path = tmp_path / "CHANGELOG_AGENT.md"
    path.write_text(
        "noise\n"
        "2026-07-22T22:00:00Z | Tier1 | T1-83 foo | bar | merged\n"
        "2026-07-22T22:10:00Z | Tier1 | T1-84 baz | qux | merged\n"
        "2026-07-22T12:00:00Z | Tier2 | should not count | x | open\n"
    )
    assert wr._count_changelog_tier1(path) == 2


def test_count_backlog_tier1_done(tmp_path) -> None:
    path = tmp_path / "BACKLOG.md"
    path.write_text(
        "### T1-01 Foo\n- Status: `done`\n\n"
        "### T1-02 Bar\n- Status: `todo`\n\n"
        "### T1-03 Baz\n- Status: `done`\n\n"
        "## Tier 2 — strategy\n### T2-01\n- Status: `done`\n"
    )
    assert wr._count_backlog_tier1_done(path) == 2


def test_count_pending_reviews_none(tmp_path) -> None:
    path = tmp_path / "PENDING_REVIEW.md"
    path.write_text(
        "# PENDING_REVIEW\n\n"
        "| Opened | Branch / PR | Summary | Evidence |\n"
        "|--------|-------------|---------|----------|\n"
        "| _(none)_ | | | |\n"
    )
    assert wr._count_pending_reviews(path) == 0


def test_count_pending_reviews_rows(tmp_path) -> None:
    path = tmp_path / "PENDING_REVIEW.md"
    path.write_text(
        "| Opened | Branch / PR | Summary | Evidence |\n"
        "|--------|-------------|---------|----------|\n"
        "| 2026-07-22 | pr/1 | C-01 | pack |\n"
        "| 2026-07-23 | pr/2 | C-02 | pack |\n"
    )
    assert wr._count_pending_reviews(path) == 2
