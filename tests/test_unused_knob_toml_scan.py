"""Tests for unused StrategyProfile knobs set in strategy TOML."""

from __future__ import annotations

from pathlib import Path

from scripts.unused_knob_toml_scan import scan_toml


def test_scan_toml_finds_unused_knobs(tmp_path: Path) -> None:
    path = tmp_path / "strategy.toml"
    path.write_text(
        """
[profiles.live-tiny]
base_spread_ticks = 2
exit_urgency_s = 600
end_date_taper_days = 7
event_sweep_levels = 2
"""
    )
    hits = scan_toml(path, {"exit_urgency_s", "end_date_taper_days", "event_sweep_levels"})
    knobs = {h["knob"] for h in hits}
    assert knobs == {"exit_urgency_s", "end_date_taper_days", "event_sweep_levels"}
    assert all(h["profile"] == "live-tiny" for h in hits)


def test_scan_toml_ignores_used_knobs(tmp_path: Path) -> None:
    path = tmp_path / "strategy.toml"
    path.write_text("[p]\nbase_spread_ticks = 2\n")
    assert scan_toml(path, {"exit_urgency_s"}) == []
