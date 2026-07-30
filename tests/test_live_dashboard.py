"""Live localhost dashboard tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

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
    assert "Quotes / fill" in html
    assert "exposure-bar" in html
    assert 'id="links"' in html
    assert "visibilityState" in html
    assert "document.title" in html
    assert "replaceState" in html
    assert "#pulse" in html or '"#" + name' in html or "'#' + name" in html
    assert "regime-summary" in html
    assert 'e.key === "?"' in html or "Shift" in html
    assert "book-filter-inv" in html
    assert "book-filters" in html
    assert "pnl-kill-bar" in html
    assert "paintAge" in html
    assert "#nav button" in html
    assert 'e.key === "Escape"' in html
    assert "AbortController" in html
    assert "const esc" in html
    assert "condition_id || marketId" in html


def test_rendered_app_javascript_is_syntax_valid() -> None:
    """Catch browser-breaking JS errors that string-presence tests cannot see."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; Python dashboard checks still run")
    html = render_app_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    checked = subprocess.run(
        [node, "--check", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_metrics_bits_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from polymaker.metrics import live_dashboard as ld

    metrics = tmp_path / "m.jsonl"
    row = {
        "ts": 1_000_000.0,
        "event": "quote",
        "condition_id": "0xc1",
        "side": "BUY",
        "price": 0.48,
        "size": 10,
        "in_reward_band": True,
        "inventory_net": 0,
    }
    metrics.write_text(json.dumps(row) + "\n")
    ld._METRICS_CACHE.clear()
    calls = {"n": 0}

    import polymaker.metrics.analyze as analyze_mod

    orig = analyze_mod.analyze

    def wrapped(path):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(analyze_mod, "analyze", wrapped)
    a = ld._metrics_bits(metrics)
    b = ld._metrics_bits(metrics)
    assert a["n_quote"] == b["n_quote"] == 1
    assert calls["n"] == 1  # second hit served from TTL/mtime cache


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


def test_insights_global_halt() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=False,
        risk_extra={"global_halt": True, "halt_reason": "daily_loss -50"},
    )
    tips = build_insights(snap)
    assert any("halt" in t.lower() for t in tips)


def test_paths_snapshot_escalates_global_halt() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=False,
        risk_extra={"global_halt": True, "halt_reason": "daily_loss -50"},
    )
    assert snap.health == "CRITICAL"
    assert "daily_loss" in snap.health_detail


def test_insights_exposure_taper() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
        risk_extra={"exposure_frac": 0.82, "exposure_usdc": 820, "max_total_exposure_usdc": 1000},
    )
    tips = build_insights(snap)
    assert any("exposure" in t.lower() for t in tips)


def test_insights_high_churn() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
    )
    snap.n_quote = 200
    snap.n_cancel = 180
    tips = build_insights(snap)
    assert any("cancel/quote" in t.lower() for t in tips)


def test_insights_many_halted() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
        live_markets=[
            {"id": "a", "regime": "HALTED", "inventory_net": 0, "reward_accrual": 0},
            {"id": "b", "regime": "HALTED", "inventory_net": 0, "reward_accrual": 0},
            {"id": "c", "regime": "QUIET", "inventory_net": 0, "reward_accrual": 0},
        ],
        risk_extra={"halted_markets": 2},
    )
    tips = build_insights(snap)
    assert any("halted" in t.lower() for t in tips)


def test_insights_stale_metrics() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
        risk_extra={"running": True},
    )
    snap.metrics_age_s = 200.0
    tips = build_insights(snap)
    assert any("metrics log quiet" in t.lower() for t in tips)


def test_insights_adverse_markout() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
    )
    snap.n_fill = 12
    snap.markout = {"30s": -0.012, "120s": -0.008}
    tips = build_insights(snap)
    assert any("markout" in t.lower() for t in tips)


def test_insights_zero_capital() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
        capital_usdc=0,
        risk_extra={"running": True},
    )
    tips = build_insights(snap)
    assert any("capital" in t.lower() for t in tips)


def test_insights_order_errors() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=False,
        risk_extra={"order_error_rate": 0.25, "order_attempts": 40},
    )
    tips = build_insights(snap)
    assert any("error rate" in t.lower() for t in tips)


def test_insights_critical_outrank_soft() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=True,
        risk_extra={"global_halt": True, "halt_reason": "manual_kill"},
    )
    snap.n_quote = 200
    snap.n_fill = 0
    tips = build_insights(snap)
    assert tips
    assert "halt" in tips[0].lower()


def test_require_loopback_host() -> None:
    from polymaker.metrics.live_dashboard import _require_loopback_host

    assert _require_loopback_host("127.0.0.1") == "127.0.0.1"
    assert _require_loopback_host("localhost") == "localhost"
    try:
        _require_loopback_host("0.0.0.0")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_insights_market_ws_down() -> None:
    snap = build_snapshot_from_paths(
        db_path=Path("/no/such.db"),
        log_dir=Path("/no/such"),
        metrics_log=Path("/no/such.jsonl"),
        paper=False,
    )
    snap.links = {"market_ws": "down", "user_ws": "up", "heartbeat": "up", "outage": "clear"}
    tips = build_insights(snap)
    assert any("market ws" in t.lower() for t in tips)


def test_engine_snapshot_links_paper() -> None:
    from types import SimpleNamespace

    from polymaker.metrics.live_dashboard import build_snapshot_from_engine

    yes = SimpleNamespace(token_id="y1")
    no = SimpleNamespace(token_id="n1")
    meta = SimpleNamespace(
        slug="test-mkt",
        yes=yes,
        no=no,
        question="Will X happen?",
        tick_size=0.01,
        rewards_min_size=5,
    )
    eng = SimpleNamespace(
        cfg=SimpleNamespace(
            paths=SimpleNamespace(db="/no/such.db", log_dir="/tmp"),
            risk=SimpleNamespace(daily_loss_kill_usdc=50.0, heartbeat_halt_failures=3),
            engine=SimpleNamespace(heartbeat=True),
        ),
        paper=True,
        _effective_capital=100.0,
        metas={"0xcid": meta},
        state=SimpleNamespace(position=lambda _t: SimpleNamespace(size=0.0)),
        regime_m={},
        _halted=set(),
        _llm_paused=set(),
        _last_regime={},
        _running=True,
        risk=None,
        md=SimpleNamespace(connected=True),
        user=None,
        gateway=None,
        _started_at=1_700_000_000.0,
        _live_dashboard=SimpleNamespace(url="http://127.0.0.1:8765/"),
    )
    snap = build_snapshot_from_engine(eng)
    assert snap.links["market_ws"] == "up"
    assert snap.links["user_ws"] == "n/a"
    assert snap.links["heartbeat"] == "n/a"
    assert isinstance(snap.links.get("uptime_s"), int)
    assert snap.url_hint.startswith("http://")


def test_engine_snapshot_uses_last_regime() -> None:
    """Book layout should show cached requote regime, not always QUIET."""
    from types import SimpleNamespace

    from polymaker.metrics.live_dashboard import build_snapshot_from_engine

    yes = SimpleNamespace(token_id="y1")
    no = SimpleNamespace(token_id="n1")
    meta = SimpleNamespace(
        slug="test-mkt",
        yes=yes,
        no=no,
        question="Will X happen?",
        tick_size=0.01,
        rewards_min_size=5,
    )
    state = SimpleNamespace(
        position=lambda _t: SimpleNamespace(size=0.0),
    )
    eng = SimpleNamespace(
        cfg=SimpleNamespace(
            paths=SimpleNamespace(db="/no/such.db", log_dir="/tmp"),
            risk=SimpleNamespace(daily_loss_kill_usdc=50.0),
        ),
        paper=True,
        _effective_capital=100.0,
        metas={"0xcid": meta},
        state=state,
        regime_m={},
        _halted=set(),
        _llm_paused=set(),
        _last_regime={"0xcid": "TRENDING"},
        _running=True,
        risk=None,
    )
    snap = build_snapshot_from_engine(eng)
    assert snap.markets
    assert snap.markets[0]["regime"] == "TRENDING"


def test_live_server_snapshot_endpoint(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics-paper.jsonl"
    t0 = 1_000_000.0
    rows = [
        {
            "ts": t0,
            "event": "quote",
            "condition_id": "0xc1",
            "side": "BUY",
            "price": 0.48,
            "size": 10,
            "in_reward_band": True,
            "inventory_net": 0,
        },
        {
            "ts": t0 + 1,
            "event": "fill",
            "condition_id": "0xc1",
            "side": "BUY",
            "price": 0.48,
            "size": 5,
            "mid": 0.5,
            "inventory_net": 5,
        },
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
            hz = json.loads(resp.read().decode())
            assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert hz["ok"] is True
        assert hz.get("mode") == "PAPER"
        assert "health" in hz
        assert "version" in hz
    finally:
        dash.stop()


def test_live_server_ephemeral_port_is_reachable() -> None:
    def snap() -> dict:
        return {"ts": time.time(), "health": "OK", "mode": "PAPER"}

    dash = LiveDashboard(snap, host="127.0.0.1", port=0, open_browser=False)
    url = dash.start()
    try:
        assert ":0/" not in url
        with urllib.request.urlopen(url + "healthz", timeout=2) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode())["ok"] is True
    finally:
        dash.stop()


def test_healthz_fails_when_snapshot_is_unavailable() -> None:
    def bad_snap() -> dict:
        raise RuntimeError("boom")

    dash = LiveDashboard(bad_snap, host="127.0.0.1", port=0, open_browser=False)
    url = dash.start()
    try:
        for path in ("api/snapshot", "healthz"):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(url + path, timeout=2)
            assert exc_info.value.code == 503
            assert json.loads(exc_info.value.read().decode())["error"] == "snapshot unavailable"
    finally:
        dash.stop()


def test_healthz_fails_for_stale_snapshot_data() -> None:
    def stale_snap() -> dict:
        return {"ts": time.time() - 60, "health": "OK", "mode": "PAPER"}

    dash = LiveDashboard(stale_snap, host="127.0.0.1", port=0, open_browser=False)
    url = dash.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url + "healthz", timeout=2)
        assert exc_info.value.code == 503
        body = json.loads(exc_info.value.read().decode())
        assert body["error"] == "snapshot data is stale"
        assert body["source_age_s"] >= 60
    finally:
        dash.stop()


def test_healthz_critical_returns_503(tmp_path: Path) -> None:
    def snap() -> dict:
        s = build_snapshot_from_paths(
            db_path=tmp_path / "state.db",
            log_dir=tmp_path,
            metrics_log=tmp_path / "m.jsonl",
            paper=False,
            risk_extra={"global_halt": True, "halt_reason": "daily_loss"},
        )
        s.health = "CRITICAL"
        s.health_detail = "Risk halt: daily_loss"
        return s.as_dict()

    dash = LiveDashboard(snap, host="127.0.0.1", port=28766, open_browser=False)
    url = dash.start()
    try:
        try:
            urllib.request.urlopen(url + "healthz", timeout=2)
            raise AssertionError("expected HTTPError 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            body = json.loads(exc.read().decode())
            assert body["ok"] is False
            assert body["health"] == "CRITICAL"
    finally:
        dash.stop()
