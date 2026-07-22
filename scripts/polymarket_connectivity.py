#!/usr/bin/env python3
"""Probe Polymarket REST + market WS connectivity (Tier-1 ops).

Separates local collector faults from upstream outages while paper runtime
accumulates toward the 24h Tier-2 gate. Does not change strategy math.

Usage:
  uv run python scripts/polymarket_connectivity.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_REST = (
    "https://clob.polymarket.com/time",
    "https://gamma-api.polymarket.com/markets?limit=1",
)
DEFAULT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _probe_rest(url: str, timeout_s: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read(120)
            return {
                "url": url,
                "ok": True,
                "status": getattr(resp, "status", None),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "body_prefix": body[:60].decode("utf-8", errors="replace"),
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "url": url,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


async def _probe_ws(url: str, timeout_s: float) -> dict[str, Any]:
    try:
        import websockets
    except ImportError:
        return {"url": url, "ok": False, "error": "websockets_not_installed"}

    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_s + 2):
            async with websockets.connect(url, open_timeout=timeout_s) as ws:
                addr = ws.remote_address
                await ws.close()
        return {
            "url": url,
            "ok": True,
            "remote": list(addr) if isinstance(addr, tuple) else str(addr),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "url": url,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout-s", type=float, default=10.0)
    ap.add_argument("--skip-ws", action="store_true")
    args = ap.parse_args()

    rest = [_probe_rest(u, args.timeout_s) for u in DEFAULT_REST]
    ws: dict[str, Any] | None = None
    if not args.skip_ws:
        ws = asyncio.run(_probe_ws(DEFAULT_WS, args.timeout_s))

    rest_ok = all(r.get("ok") for r in rest)
    ws_ok = True if ws is None else bool(ws.get("ok"))
    if rest_ok and ws_ok:
        overall = "OK"
        rc = 0
    elif not rest_ok and not ws_ok:
        overall = "DOWN"
        rc = 1
    else:
        overall = "DEGRADED"
        rc = 1

    payload = {
        "status": overall,
        "rest": rest,
        "ws": ws,
        "rest_ok": rest_ok,
        "ws_ok": ws_ok,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(
        f"status={overall} rest_ok={rest_ok} ws_ok={ws_ok}",
        file=sys.stderr,
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
