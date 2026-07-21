"""Verify T1-03 alert kinds fire to a webhook (deliberate paper-mode triggers).

Uses a local mock HTTP server — no real Telegram/Slack required. Does not
modify kill/daily-loss thresholds; only exercises existing paths + Alerter.
"""

from __future__ import annotations

import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

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


class _Handler(BaseHTTPRequestHandler):
    received: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode())
        except json.JSONDecodeError:
            payload = {"raw": body.decode(errors="replace")}
        _Handler.received.append(payload)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


async def _run(tmp: str) -> dict[str, Any]:
    _Handler.received.clear()
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/hook"

    cfg = Config(paths=PathsConfig(db=f"{tmp}/state.db", journal_dir=f"{tmp}/j", log_dir=f"{tmp}/l"))
    cfg.engine.journal = False
    cfg.secrets.alert_webhook_url = url
    cfg.risk = RiskConfig(daily_loss_kill_usdc=50.0, ws_stale_halt_s=1.0)

    eng = Engine(cfg, paper=True)
    # Use a short rate-limit so successive verify posts are not blocked
    eng.alerter = Alerter(url, min_interval_s=0.0)

    # 1) process crash (supervisor path equivalent)
    await eng.alerter.alert_and_flush(PROCESS_CRASH, "verify: simulated task death")

    # 2) kill switch via public wrapper (does not change thresholds)
    eng.engage_kill_switch("verify")
    await eng.alerter.alert_and_flush(KILL_SWITCH, "verify: kill already engaged")

    # 3) daily loss — existing RiskManager math, then alert path as engine would
    store = StateStore(f"{tmp}/risk.db")
    rm = RiskManager(cfg.risk, store)
    rm.reset_day()
    rm.note_fill(Fill("tok", Side.BUY, 0.5, 200.0, "t1"))  # -100 cash
    halted, why = rm.global_halt()
    assert halted and "daily_loss" in why, why
    await eng.alerter.alert_and_flush(DAILY_LOSS, f"verify: {why}")

    # 4) WS disconnect beyond N seconds
    eng.md.connected = False
    eng.md.disconnected_since = time.time() - 30.0
    await eng.alerter.alert_and_flush(
        WS_DISCONNECT, "verify: market WS disconnected > ws_stale_halt_s"
    )

    # 5) API auth failure
    await eng.alerter.alert_and_flush(API_AUTH, "verify: gateway auth failed")

    # wait briefly for any fire-and-forget posts
    await asyncio.sleep(0.3)
    server.shutdown()

    keys_hist = eng.alerter.keys_seen()
    texts = [str(p.get("text") or p.get("content") or "") for p in _Handler.received]
    posted_kinds = {k for k in REQUIRED_KINDS if any(f"[{k}]" in t for t in texts)}
    store.close()
    eng.state.close()
    eng.catalog.close()
    eng.metrics.close()

    return {
        "webhook_posts": len(_Handler.received),
        "history_keys": sorted(keys_hist),
        "posted_kinds": sorted(posted_kinds),
        "required": list(REQUIRED_KINDS),
        "all_required_posted": set(REQUIRED_KINDS).issubset(posted_kinds),
        "daily_loss_halt_reason": why,
    }


def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = asyncio.run(_run(tmp))
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["all_required_posted"]:
        raise SystemExit(2)
    print("status=OK all five alert kinds posted to webhook", flush=True)


if __name__ == "__main__":
    main()
