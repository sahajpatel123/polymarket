"""T1-08 metrics dashboard tests."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.metrics.analyze import analyze
from polymaker.metrics.dashboard import render_dashboard, write_dashboard


def _fixture(path: Path) -> None:
    t0 = 1_000_000.0
    rows = [
        {"ts": t0, "event": "market_meta", "condition_id": "0xc1",
         "rewards_daily_rate": 86.4},
        {"ts": t0, "event": "mark", "condition_id": "0xc1", "fv": 0.5,
         "inventory_net": 0},
        {"ts": t0 + 1, "event": "quote", "condition_id": "0xc1", "side": "BUY",
         "price": 0.48, "size": 10, "in_reward_band": True, "inventory_net": 0},
        {"ts": t0 + 10, "event": "fill", "condition_id": "0xc1", "side": "BUY",
         "price": 0.48, "size": 10, "mid": 0.50, "inventory_net": 10},
        {"ts": t0 + 40, "event": "mark", "condition_id": "0xc1", "fv": 0.49,
         "inventory_net": 10},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_render_contains_key_health_fields(tmp_path: Path) -> None:
    log = tmp_path / "m.jsonl"
    _fixture(log)
    html = render_dashboard(analyze(log))
    assert "Quotes" in html
    assert "Fills" in html
    assert "Realized spread" in html
    assert "Adverse selection" in html
    assert "0xc1" in html


def test_write_dashboard_file(tmp_path: Path) -> None:
    log = tmp_path / "m.jsonl"
    out = tmp_path / "dashboard.html"
    _fixture(log)
    rep = write_dashboard(log, out)
    assert out.exists()
    text = out.read_text()
    assert "polymaker metrics" in text
    assert rep.n_quote == 1
    assert rep.n_fill == 1
