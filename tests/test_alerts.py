"""T1-03 alerting wrapper tests."""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import pytest

from polymaker.alerts import (
    API_AUTH,
    DAILY_LOSS,
    KILL_SWITCH,
    PROCESS_CRASH,
    REQUIRED_KINDS,
    WS_DISCONNECT,
    Alerter,
)
from polymaker.config import Config, PathsConfig, RiskConfig
from polymaker.domain import Fill, Side
from polymaker.engine import Engine
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore


class _H(BaseHTTPRequestHandler):
    posts: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n).decode())
        _H.posts.append(str(body.get("text") or ""))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


@pytest.fixture
def webhook_url():
    _H.posts.clear()
    srv = HTTPServer(("127.0.0.1", 0), _H)
    Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/h"
    srv.shutdown()


async def test_alerter_posts_critical_kinds(webhook_url: str) -> None:
    a = Alerter(webhook_url, min_interval_s=0.0)
    for k in REQUIRED_KINDS:
        ok = await a.alert_and_flush(k, f"msg-{k}", critical=True)
        assert ok
    await asyncio.sleep(0.1)
    assert len(_H.posts) == 5
    for k in REQUIRED_KINDS:
        assert any(f"[{k}]" in p for p in _H.posts)


async def test_engine_wires_kill_and_daily_loss_alert_keys(tmp_path, webhook_url: str) -> None:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "s.db"),
                                   journal_dir=str(tmp_path / "j"),
                                   log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    cfg.secrets.alert_webhook_url = webhook_url
    eng = Engine(cfg, paper=True)
    eng.alerter = Alerter(webhook_url, min_interval_s=0.0)

    eng.engage_kill_switch("test")
    assert KILL_SWITCH in eng.alerter.keys_seen()
    halted, why = eng.risk.global_halt()
    assert halted and "kill" in why

    store = StateStore(str(tmp_path / "r.db"))
    rm = RiskManager(RiskConfig(daily_loss_kill_usdc=50), store)
    rm.reset_day()
    rm.note_fill(Fill("t", Side.BUY, 0.5, 200.0, "x"))
    h2, why2 = rm.global_halt()
    assert h2 and "daily_loss" in why2
    await eng.alerter.alert_and_flush(DAILY_LOSS, why2)
    assert DAILY_LOSS in eng.alerter.keys_seen()
    store.close()
    eng.state.close()
    eng.catalog.close()
    eng.metrics.close()


def test_required_kinds_stable() -> None:
    assert set(REQUIRED_KINDS) == {
        PROCESS_CRASH, KILL_SWITCH, DAILY_LOSS, WS_DISCONNECT, API_AUTH
    }
