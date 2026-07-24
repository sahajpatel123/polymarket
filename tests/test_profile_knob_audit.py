"""Tests for StrategyProfile knob usage audit."""

from __future__ import annotations

from pathlib import Path

from polymaker.strategy.knob_audit import audit_profile_knobs


def test_audit_flags_known_unused_knobs() -> None:
    rep = audit_profile_knobs()
    # The audit only knows about fields defined on StrategyProfile.
    # Removing dead knobs (exit_urgency_s, end_date_taper_days, event_sweep_levels)
    # from the model means they no longer appear in the audit at all.
    assert "gamma" in rep.used
    assert "trend_vol_ratio" in rep.used
    assert "reprice_ticks" in rep.used
    assert rep.scanned_files


def test_audit_tmp_tree_detects_only_referenced(tmp_path: Path) -> None:
    py = tmp_path / "fake.py"
    py.write_text("def f(p):\n    return p.gamma + p.c_tox\n")
    rep = audit_profile_knobs([tmp_path])
    assert "gamma" in rep.used
    assert "c_tox" in rep.used
    # exit_urgency_s was removed from the model, so it's not in unused either
    assert "exit_urgency_s" not in rep.used
