"""Smoke for min_edge EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_min_edge_ticks_default():
    assert StrategyProfile().min_edge_ticks == 1
