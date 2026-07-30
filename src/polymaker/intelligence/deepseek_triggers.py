"""Grok-set automated triggers — execute without API calls.

Grok defines triggers on the 10-min oversight cycle. The engine
evaluates them on EVERY requote (sub-second). When a trigger fires,
the pre-specified action applies immediately — zero API cost.

Grok writes the rules; the engine enforces them 24/7.

Example trigger set by Grok:
    {"condition": "tox_above", "threshold": 0.3, "action": "pause"}
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ── Trigger conditions (keys match oversigt snapshot fields) ─────────

TRIGGER_CONDITIONS = frozenset({
    "tox_above",          # toxicity exceeds threshold
    "vol_above",          # vol_ratio exceeds threshold
    "flow_above",         # flow_z magnitude exceeds threshold
    "drawdown_above",     # portfolio drawdown % exceeds threshold
    "daily_loss_above",   # daily PnL drops below -threshold
    "reward_below",       # reward_rate per market below threshold
    "spread_below",       # spread too tight for safety
    "spread_above",       # spread too wide (dead book)
})

# Actions trigger can execute automatically (zero API call)
TRIGGER_ACTIONS = frozenset({
    "pause",              # halt quoting on this market
    "defensive",          # push band down + size down 0.7x
    "size_down",          # reduce aggression by mult
    "size_up",            # increase aggression (when conditions clear)
    "alert_only",         # log + flag for Grok review
})


@dataclass
class DeepSeekTrigger:
    """One automated guardrail set by Grok opinion."""

    condition: str          # one of TRIGGER_CONDITIONS
    threshold: float        # value that triggers the action
    action: str             # one of TRIGGER_ACTIONS
    market: str             # cid ("" = portfolio-level)
    mult: float = 0.7       # multiplier for size_up/down
    fired_count: int = 0
    last_fired_ts: float = 0.0
    enabled: bool = True
    reason: str = ""
    set_by: str = "grok"    # grok | manual

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "threshold": self.threshold,
            "action": self.action,
            "market": self.market[:8] if self.market else "",
            "mult": self.mult,
            "fired_count": self.fired_count,
            "last_fired_ts": self.last_fired_ts,
            "enabled": self.enabled,
            "reason": self.reason,
            "set_by": self.set_by,
        }


@dataclass
class TriggerViolation:
    """One triggered condition — what fired, when, what we did."""

    trigger: DeepSeekTrigger
    current_value: float
    fired_ts: float
    action_taken: str         # what the engine applied
    engine_result: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition": self.trigger.condition,
            "threshold": self.trigger.threshold,
            "current_value": round(self.current_value, 4),
            "fired_ts": self.fired_ts,
            "action_taken": self.action_taken,
            "engine_result": self.engine_result,
            "market": self.trigger.market[:8] if self.trigger.market else "",
        }


def evaluate_triggers(
    triggers: list[DeepSeekTrigger],
    snapshot: dict[str, Any],
    *,
    cooldown_s: float = 300.0,
) -> list[TriggerViolation]:
    """Check all triggers against the snapshot. Return violations.

    Each trigger has a 5-min cooldown after firing to avoid flap.

    Call this on every requote + every trade print. O(triggers) —
    negligible cost, no API calls.
    """
    now = time.time()
    fired: list[TriggerViolation] = []
    for t in triggers:
        if not t.enabled:
            continue
        if now - t.last_fired_ts < cooldown_s:
            continue
        val = _extract_value(t.condition, t.market, snapshot)
        if val is None:
            continue
        triggered = False
        if t.condition in ("tox_above", "vol_above", "flow_above",
                           "drawdown_above", "spread_above", "spread_below"):
            triggered = val > t.threshold
        elif t.condition in ("daily_loss_above",):
            triggered = val < -t.threshold
        elif t.condition in ("reward_below",):
            triggered = val < t.threshold
        if not triggered:
            continue
        t.fired_count += 1
        t.last_fired_ts = now
        violation = TriggerViolation(
            trigger=t,
            current_value=val,
            fired_ts=now,
            action_taken=t.action,
        )
        fired.append(violation)
    return fired


def _extract_value(
    condition: str, market: str, snapshot: dict[str, Any]
) -> float | None:
    """Pull the measured value for a condition from the snapshot."""
    mkts = snapshot.get("markets", {})
    if condition == "tox_above":
        return float(mkts.get(market[:8], {}).get("toxicity", 0) or 0)
    if condition == "vol_above":
        return float(mkts.get(market[:8], {}).get("vol_ratio", 0) or 0)
    if condition == "flow_above":
        return abs(float(mkts.get(market[:8], {}).get("flow_z", 0) or 0))
    if condition == "spread_below":
        return float(mkts.get(market[:8], {}).get("spread_ticks", 0) or 99)
    if condition == "spread_above":
        return float(mkts.get(market[:8], {}).get("spread_ticks", 0) or 0)
    if condition == "drawdown_above":
        return float(snapshot.get("drawdown_pct", 0) or 0)
    if condition == "daily_loss_above":
        return abs(float(snapshot.get("daily_pnl", 0) or 0))
    if condition == "reward_below":
        return float(mkts.get(market[:8], {}).get("reward_daily_rate", 0) or 999)
    return None
