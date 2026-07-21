"""Critical-event alerting via a generic webhook POST.

Fire-and-forget, deduped, and rate-limited so a flapping condition can't spam.
Works with any endpoint that accepts a JSON POST (Discord/Slack/Telegram-bridge/
ntfy). No-ops cleanly when no webhook is configured, so callers never branch.

Tier-1 alert kinds (T1-03) — each must be verifiable via scripts/verify_alerts.py:
  process_crash, kill_switch, daily_loss, ws_disconnect, api_auth
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from polymaker.logging import get_logger

log = get_logger("alerts")

# Stable keys used by engine + verify script (do not rename lightly).
PROCESS_CRASH = "process_crash"
KILL_SWITCH = "kill_switch"
DAILY_LOSS = "daily_loss"
WS_DISCONNECT = "ws_disconnect"
API_AUTH = "api_auth"

REQUIRED_KINDS = (PROCESS_CRASH, KILL_SWITCH, DAILY_LOSS, WS_DISCONNECT, API_AUTH)


@dataclass
class AlertRecord:
    key: str
    message: str
    critical: bool
    ts: float = field(default_factory=time.time)


class Alerter:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        min_interval_s: float = 30.0,
        proxy: str | None = None,
    ) -> None:
        self._url = webhook_url
        self._min_interval = min_interval_s
        self._proxy = proxy
        self._last_sent: dict[str, float] = {}  # key -> ts (dedupe/rate-limit)
        self.history: list[AlertRecord] = []  # in-process record for tests/verify

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def alert(self, key: str, message: str, *, critical: bool = False) -> None:
        """Queue an alert. `key` dedupes/rate-limits repeated conditions.

        Always logs (so nothing is lost even without a webhook); posts to the
        webhook at most once per `min_interval_s` per key (critical bypasses the
        limit). Safe to call from sync code — schedules the POST on the loop.
        """
        self.history.append(AlertRecord(key=key, message=message, critical=critical))
        (log.critical if critical else log.warning)("alert", key=key, msg=message)
        if not self._url:
            return
        now = time.time()
        if not critical and now - self._last_sent.get(key, 0.0) < self._min_interval:
            return
        self._last_sent[key] = now
        with contextlib.suppress(RuntimeError):  # no running loop (off-loop call)
            asyncio.get_running_loop().create_task(self._post(key, message, critical))

    async def alert_and_flush(self, key: str, message: str, *, critical: bool = True) -> bool:
        """Await the webhook POST (for verify scripts). Returns True if posted OK."""
        self.history.append(AlertRecord(key=key, message=message, critical=critical))
        (log.critical if critical else log.warning)("alert", key=key, msg=message)
        if not self._url:
            return False
        self._last_sent[key] = time.time()
        return await self._post(key, message, critical)

    async def _post(self, key: str, message: str, critical: bool) -> bool:
        text = f"{'CRITICAL' if critical else 'WARN'} polymaker [{key}] {message}"
        try:
            kwargs: dict[str, Any] = {"timeout": 10.0}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            async with httpx.AsyncClient(**kwargs) as c:
                # send both keys so Slack ("text") and Discord ("content") work
                r = await c.post(self._url, json={"text": text, "content": text})
                r.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("alert_post_failed", err=str(exc))
            return False

    def keys_seen(self) -> set[str]:
        return {r.key for r in self.history}
