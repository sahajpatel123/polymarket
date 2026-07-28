"""Live localhost dashboard tests."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from polymaker.metrics.live_dashboard import (
    LiveDashboard,
    build_insights,
    build_snapshot_from_paths,
    render_app_html,
)


def test_render_app_has_layouts() -> None:
    html = render_app_html()
    assert "Pulse" in html
    assert "Book" in html
    assert "Risk" in html
    assert "Tape" in html
    assert "/api/snapshot" in html
    assert "Polymaker" in html or "polymaker" in html.lower()


def test_snapshot_empty_paths(tmp_path: Path) -> None:
    snap = build_snapshot_from_paths(
        db_path=tmp_path / "missing.db",
        log_dir=tmp_path,
        metrics_log=tmp_path / "m.jsonl",
        paper=True,
        capital_usdc=500,
    )
    assert snap.mode == "PAPER"
    assert snap.health in {"NO_DATA", "OK", "WARN", "CRITICAL", "ACTIVE"}
    assert snap.capital_usdc == 500
    assert snap.insights
    d = snap.as_dict()
    assert "n_quote" in d


def test_insights_paper_no_fills() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
    )
    snap.n_quote = 100
    snap.n_fill = 0
    snap.mode = "PAPER"
    tips = build_insights(snap)
    assert any("no fills" in t.lower() for t in tips)


def test_live_server_snapshot_endpoint(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics-paper.jsonl"
    t0 = 1_000_000.0
    rows = [
        {"ts": t0, "event": "quote", "condition_id": "0xc1", "side": "BUY",
         "price": 0.48, "size": 10, "in_reward_band": True, "inventory_net": 0},
        {"ts": t0 + 1, "event": "fill", "condition_id": "0xc1", "side": "BUY",
         "price": 0.48, "size": 5, "mid": 0.5, "inventory_net": 5},
    ]
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def snap() -> dict:
        return build_snapshot_from_paths(
            db_path=tmp_path / "state.db",
            log_dir=tmp_path,
            metrics_log=metrics,
            paper=True,
            capital_usdc=100,
        ).as_dict()

    dash = LiveDashboard(snap, host="127.0.0.1", port=28765, open_browser=False)
    url = dash.start()
    try:
        with urllib.request.urlopen(url + "api/snapshot", timeout=2) as resp:
            data = json.loads(resp.read().decode())
        assert data["mode"] == "PAPER"
        assert data["n_quote"] >= 1
        assert data["n_fill"] >= 1
        with urllib.request.urlopen(url, timeout=2) as resp:
            html = resp.read().decode()
        assert "Pulse" in html
        with urllib.request.urlopen(url + "healthz", timeout=2) as resp:
            assert json.loads(resp.read().decode())["ok"] is True
    finally:
        dash.stop()
