"""Tests for ProfileHistory append / list / get_at / rollback."""

from __future__ import annotations

from pathlib import Path

from polymaker.intelligence.profile_history import ProfileHistory


def test_append_and_list_recent(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "ph.db")
    hist.append(
        old_profile={"gamma": 0.5},
        new_profile={"gamma": 0.7},
        source="self_improve",
        reason="widen skew",
        paper_validated=True,
        ts=100.0,
    )
    hist.append(
        old_profile={"gamma": 0.7},
        new_profile={"gamma": 0.8, "c_vol": 1.5},
        source="manual",
        reason="manual tweak",
        ts=200.0,
    )
    recent = hist.list_recent(10)
    assert len(recent) == 2
    assert recent[0].ts == 200.0
    assert recent[0].source == "manual"
    assert recent[0].diff() == {"c_vol": (None, 1.5), "gamma": (0.7, 0.8)}
    hist.close()


def test_get_at_and_rollback(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "ph.db")
    hist.append(
        old_profile={"gamma": 0.5, "c_vol": 1.2},
        new_profile={"gamma": 0.9, "c_vol": 1.2},
        source="self_improve",
        reason="bad change",
        paper_validated=True,
        ts=1000.0,
    )
    hist.append(
        old_profile={"gamma": 0.9, "c_vol": 1.2},
        new_profile={"gamma": 0.9, "c_vol": 2.0},
        source="hot_reload",
        reason="hot",
        ts=2000.0,
    )
    at = hist.get_at(1500.0)
    assert at is not None
    assert at.ts == 1000.0
    assert at.new_profile["gamma"] == 0.9

    restored = hist.rollback(1000.0, reason="undo bad")
    assert restored == {"gamma": 0.5, "c_vol": 1.2}
    latest = hist.latest()
    assert latest is not None
    assert latest.source == "rollback"
    assert latest.new_profile == {"gamma": 0.5, "c_vol": 1.2}
    hist.close()


def test_rollback_missing_raises(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "ph.db")
    try:
        hist.rollback(1.0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    hist.close()
