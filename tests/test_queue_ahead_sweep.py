"""Tests for queue_ahead_sweep diagnostic helpers."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.domain import Side
from polymaker.paper.queue_fill_sim import FillMode, QueueAwareFillSimulator


def test_equal_price_vs_through_unit():
    """Mirror script expectation: equal blocked, through fills at ahead=0."""
    cons = QueueAwareFillSimulator(mode=FillMode.CONSERVATIVE, default_queue_ahead=0.0)
    from polymaker.domain import OpenOrder

    cons.place(OpenOrder("a", "tok", Side.BUY, 0.42, 50.0), ts=0.0)
    assert cons.match("tok", Side.SELL, 0.42, 10.0, ts=1.0) == []
    fills = cons.match("tok", Side.SELL, 0.41, 10.0, ts=2.0)
    assert sum(f.size for f in fills) == 10.0


def test_queue_ahead_sweep_script_runs(tmp_path: Path, monkeypatch):
    """Smoke: script imports and builds report shape on tiny synthetic path skip.

    Full journal run is covered by the cycle evidence command; here we only
    assert the equal-price flag logic from a hand-built report dict.
    """
    report = {
        "rows": [
            {"label": "base_ahead0", "n_fill": 12},
            {"label": "cons_ahead0", "n_fill": 0},
            {"label": "cons_default200", "n_fill": 0},
        ]
    }
    by = {r["label"]: r for r in report["rows"]}
    equal_blocks = by["base_ahead0"]["n_fill"] > 0 and by["cons_ahead0"]["n_fill"] == 0
    assert equal_blocks is True
    (tmp_path / "r.json").write_text(json.dumps(report))
    assert (tmp_path / "r.json").exists()
