"""Continuous LLM oversight — commentary + bounded action queue.

Contract:
  - Every interval (default 30 min), gather a snapshot (PnL, drawdown, fill rate,
    top-of-book, anomalies) and call Grok via intelligence.agent.
  - LLM returns narrative + actions via the oversight_report tool.
  - Actions are queued only — never mutate engine state here.
  - Engine.apply_oversight_action is owned by another agent; we expose
    drain_actions() / peek_actions() for that wiring.
  - dry_run=true → log + queue with applied=False marker, never treated as live.

Action types: tighten_spread | widen_spread | pause_market | add_layer |
drop_market | no_op
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from polymaker.intelligence.agent import GrokAgent
from polymaker.intelligence.memory import AgentMemory
from polymaker.intelligence.prompts import (
    OVERSIGHT_TOOL_SCHEMA,
    prompt_oversight_commentary,
)

log = logging.getLogger("polymaker.intelligence.oversight")

ACTION_TYPES = frozenset(
    {
        "tighten_spread",
        "widen_spread",
        "pause_market",
        "add_layer",
        "drop_market",
        "no_op",
    }
)

DEFAULT_INTERVAL_S = 30 * 60


@dataclass
class OversightAction:
    """One bounded action proposed by the LLM (engine applies later)."""

    type: str
    market: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True
    reason: str = ""
    narrative: str = ""
    reasoning: str = ""
    ts: float = field(default_factory=time.time)
    # Set True only after a real engine apply (not by this module).
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OversightReport:
    narrative: str
    actions: list[OversightAction]
    reasoning: str
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "narrative": self.narrative,
            "actions": [a.as_dict() for a in self.actions],
            "reasoning": self.reasoning,
            "usage": self.usage,
        }


SnapshotProvider = Callable[[], dict[str, Any] | Any]


def gather_default_snapshot(
    *,
    pnl: float = 0.0,
    drawdown: float = 0.0,
    fill_rate: float = 0.0,
    top_of_book: dict[str, Any] | None = None,
    anomalies: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a snapshot dict for the 30-min commentary prompt."""
    snap: dict[str, Any] = {
        "ts": time.time(),
        "pnl": pnl,
        "drawdown": drawdown,
        "fill_rate": fill_rate,
        "top_of_book": top_of_book or {},
        "anomalies": anomalies or [],
    }
    if extra:
        snap.update(extra)
    return snap


def _normalize_actions(
    raw: list[Any],
    *,
    narrative: str,
    reasoning: str,
) -> list[OversightAction]:
    out: list[OversightAction] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "no_op").strip().lower()
        if t not in ACTION_TYPES:
            t = "no_op"
        out.append(
            OversightAction(
                type=t,
                market=item.get("market"),
                params=dict(item.get("params") or {}),
                dry_run=bool(item.get("dry_run", True)),
                reason=str(item.get("reason") or ""),
                narrative=narrative,
                reasoning=reasoning,
            )
        )
    if not out:
        out.append(
            OversightAction(
                type="no_op",
                dry_run=True,
                reason="no actions returned",
                narrative=narrative,
                reasoning=reasoning,
            )
        )
    return out


class OversightLoop:
    """Periodic commentary + action queue (no engine mutation)."""

    def __init__(
        self,
        agent: GrokAgent,
        memory: AgentMemory | None = None,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        snapshot_provider: SnapshotProvider | None = None,
        max_queue: int = 500,
    ) -> None:
        self.agent = agent
        self.memory = memory
        self.interval_s = float(interval_s)
        self.snapshot_provider = snapshot_provider or (
            lambda: gather_default_snapshot()
        )
        self._queue: deque[OversightAction] = deque(maxlen=max_queue)
        self._lock = asyncio.Lock()
        self.last_report: OversightReport | None = None
        self.cycle_count = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def peek_actions(self) -> list[OversightAction]:
        return list(self._queue)

    def drain_actions(self, *, include_dry_run: bool = True) -> list[OversightAction]:
        """Pop all queued actions for the engine consumer."""
        out: list[OversightAction] = []
        while self._queue:
            a = self._queue.popleft()
            if a.dry_run and not include_dry_run:
                log.info("oversight_dry_run_skipped type=%s reason=%s", a.type, a.reason)
                continue
            out.append(a)
        return out

    def enqueue(self, action: OversightAction) -> None:
        """Internal/test helper — never called with live engine apply here."""
        if action.dry_run:
            log.info(
                "oversight_action_dry_run type=%s market=%s reason=%s",
                action.type,
                action.market,
                action.reason,
            )
        else:
            log.info(
                "oversight_action_queued type=%s market=%s reason=%s",
                action.type,
                action.market,
                action.reason,
            )
        self._queue.append(action)

    async def run_once(self, snapshot: dict[str, Any] | None = None) -> OversightReport:
        """One oversight cycle: snapshot → LLM → queue actions."""
        snap = snapshot if snapshot is not None else self.snapshot_provider()
        if asyncio.iscoroutine(snap):
            snap = await snap  # type: ignore[misc]
        assert isinstance(snap, dict)

        mem_block = ""
        if self.memory is not None:
            mem_block = self.memory.inject_for_prompt(
                query="oversight pnl drawdown fills anomalies",
                k=8,
            )

        system, user = prompt_oversight_commentary(snap, memory_block=mem_block)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        args, resp = await self.agent.chat_json_tool(
            messages,
            tool_name="oversight_report",
            tool_schema=OVERSIGHT_TOOL_SCHEMA,
            kind="oversight",
            description="30-minute oversight report with bounded actions",
        )
        narrative = str(args.get("narrative") or resp.content or "")
        reasoning = str(args.get("reasoning") or "")
        actions = _normalize_actions(
            list(args.get("actions") or []),
            narrative=narrative,
            reasoning=reasoning,
        )

        # Persist MEMORY: lines
        if self.memory is not None and (narrative or resp.content):
            self.memory.parse_and_store_from_text(
                f"{narrative}\n{resp.content}\n{reasoning}"
            )

        async with self._lock:
            for a in actions:
                self.enqueue(a)
            report = OversightReport(
                narrative=narrative,
                actions=actions,
                reasoning=reasoning,
                usage=resp.usage.as_dict(),
            )
            self.last_report = report
            self.cycle_count += 1
        return report

    async def run_forever(self) -> None:
        """Background loop — cancelled via stop()."""
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("oversight_cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                continue

    def start_background(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="oversight_loop")
        return self._task

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()


# Explicit non-mutating apply hook documentation for Agent 3:
async def apply_actions_via_engine(
    engine: Any,
    actions: list[OversightAction],
) -> list[dict[str, Any]]:
    """Hand actions to engine.apply_oversight_action when available.

    This module never requires engine. If the method is missing, dry-run all.
    """
    results: list[dict[str, Any]] = []
    apply = getattr(engine, "apply_oversight_action", None)
    for a in actions:
        if a.dry_run or apply is None:
            results.append({"action": a.as_dict(), "status": "dry_run_or_unwired"})
            continue
        try:
            out = apply(a)
            if asyncio.iscoroutine(out):
                out = await out
            a.applied = True
            results.append({"action": a.as_dict(), "status": "applied", "result": out})
        except Exception as exc:  # noqa: BLE001
            results.append({"action": a.as_dict(), "status": "error", "error": str(exc)})
    return results
