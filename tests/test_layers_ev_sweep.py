"""Smoke for layers EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_layers_default():
    assert StrategyProfile().layers == 2
