"""Tests for end-of-day review (mocked LLM)."""

from __future__ import annotations

from pathlib import Path

from polymaker.intelligence.review import (
    DaySummary,
    LocalMemoryStore,
    ReviewResult,
    persist_review,
    render_markdown,
    run_daily_review,
    utc_review_cron_hour_minute,
)


def test_utc_schedule() -> None:
    assert utc_review_cron_hour_minute() == (23, 55)


def test_review_generates_markdown_and_memory(tmp_path: Path) -> None:
    mem = LocalMemoryStore(tmp_path / "mem.db")

    def llm(**_kwargs):
        return {
            "grade": "B",
            "top_3_problems": ["thin fills", "false TRENDING", "inventory drift"],
            "top_3_wins": ["stable rewards", "low churn", "clean risk"],
            "tomorrow_baseline_adjustments": {"trend_vol_ratio": 3.0},
            "new_memory_items": [
                "Raise trend_vol_ratio on thin books",
                "Watch Newsom reward band gap",
            ],
        }

    summary = DaySummary(
        date_utc="2026-07-27",
        pnl=12.5,
        drawdown=3.0,
        fills=8,
        markouts=[-0.01, 0.002],
        regime_history=["QUIET", "TRENDING", "QUIET"],
        self_improve_actions=["widened c_vol after decay"],
        memory_growth=2,
        hit_rate=0.55,
    )
    result = run_daily_review(
        summary,
        llm=llm,
        memory=mem,
        reviews_dir=tmp_path / "daily_reviews",
    )
    assert result.grade == "B"
    assert len(result.top_3_problems) == 3
    assert len(result.top_3_wins) == 3
    assert result.tomorrow_baseline_adjustments["trend_vol_ratio"] == 3.0
    assert result.report_path is not None
    assert result.report_path.exists()
    text = result.report_path.read_text(encoding="utf-8")
    assert "Polymaker Daily Review — 2026-07-27" in text
    assert "**Grade:** B" in text
    assert "thin fills" in text
    recent = mem.recent(10)
    assert len(recent) == 2
    assert "trend_vol_ratio" in recent[0].text or "reward band" in recent[0].text
    mem.close()


def test_render_and_persist(tmp_path: Path) -> None:
    summary = DaySummary(date_utc="2026-01-01", pnl=-1.0, fills=0)
    result = ReviewResult(
        grade="D",
        top_3_problems=["a", "b", "c"],
        top_3_wins=["x", "y", "z"],
        tomorrow_baseline_adjustments={},
        new_memory_items=[],
    )
    md = render_markdown(summary, result)
    assert "Grade:** D" in md
    path = persist_review(summary, result, reviews_dir=tmp_path)
    assert path.name == "2026-01-01.md"
    assert path.read_text(encoding="utf-8") == md


def test_from_llm_truncates_lists() -> None:
    r = ReviewResult.from_llm(
        {
            "grade": "A",
            "top_3_problems": ["1", "2", "3", "4"],
            "top_3_wins": ["a"],
            "new_memory_items": ["m"],
        }
    )
    assert r.top_3_problems == ["1", "2", "3"]
    assert r.top_3_wins == ["a"]


def test_should_run_eod_and_latest(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from polymaker.intelligence.review import (
        DaySummary, ReviewResult, gather_day_summary, persist_review, should_run_eod_review,
    )
    assert should_run_eod_review(datetime(2026, 7, 27, 23, 56, tzinfo=UTC)) is True
    assert should_run_eod_review(datetime(2026, 7, 27, 12, 0, tzinfo=UTC)) is False
    summary = gather_day_summary(tmp_path / "missing.db", date_utc="2026-07-27")
    assert summary.date_utc == "2026-07-27"
    assert summary.fills == 0
    result = ReviewResult(grade="B", top_3_problems=["a"], top_3_wins=["b"],
                          tomorrow_baseline_adjustments={}, new_memory_items=[])
    path = persist_review(summary, result, reviews_dir=tmp_path / "daily_reviews")
    assert path.exists()
    assert (tmp_path / "daily_reviews" / "LATEST.md").exists()


def test_invalid_grade_becomes_c() -> None:
    from polymaker.intelligence.review import ReviewResult
    r = ReviewResult.from_llm({"grade": "Z", "top_3_problems": [], "top_3_wins": []})
    assert r.grade == "C"
