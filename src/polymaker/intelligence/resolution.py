"""Resolution probability engine — the second profit source.

Polymarket has two independent profit engines:
1. Market making (rewards + spread) — capped, diminishing returns
2. Resolution arbitrage (mispricing) — uncapped, where the real money is

The key insight: market_price ≠ P(event). When the market is wrong,
taking a directional position to resolution earns more than any spread
or reward could. The metric:

    resolution_alpha = |market_price - P(event)| / tick_size

When alpha > 3 ticks, market-making should be biased toward accumulating
inventory in the direction of the mispricing.

This module uses DeepSeek as the probability estimator. The LLM reads a
market question and returns a calibrated probability estimate (0-1) with
a confidence score. This is cross-referenced against the current market
price to detect mispricing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("polymaker.resolution")


# ── Alpha thresholds ──────────────────────────────────────────────────

ALPHA_BIAS_THRESHOLD = 3.0     # >3 ticks = bias quoting direction
ALPHA_DIRECTIONAL_THRESHOLD = 8.0  # >8 ticks = take directional position
ALPHA_EXTREME_THRESHOLD = 15.0     # >15 ticks = max size directional
ALPHA_REFRESH_SECONDS = 3600       # recompute P(event) every hour


@dataclass
class ResolutionSignal:
    """One market's resolution arbitrage signal.

    ``alpha`` measures mispricing in ticks: how wrong the market price
    is relative to the estimated true probability. The larger alpha is,
    the more the market is mispriced, and the more we should be taking
    a directional position rather than market-making.

    ``direction``:
        "BUY_YES"  → market_price < P(event) — YES is underpriced
        "SELL_YES" → market_price > P(event) — YES is overpriced
        "NONE"     → within noise band — market is roughly correct

    ``confidence`` in [0, 1] estimates how sure DeepSeek is about its
    probability estimate. Factor this into sizing — low confidence
    means the signal might be wrong, so size small.
    """

    condition_id: str
    question: str = ""
    market_price: float = 0.5
    estimated_probability: float = 0.5
    alpha: float = 0.0
    direction: str = "NONE"
    confidence: float = 0.0
    last_updated: float = 0.0
    reasoning: str = ""
    raw_response: str = ""

    BIAS_NONE = "NONE"
    BIAS_BUY_YES = "BUY_YES"
    BIAS_SELL_YES = "SELL_YES"

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id[:8] if self.condition_id else "",
            "market_price": round(self.market_price, 4),
            "estimated_probability": round(self.estimated_probability, 4),
            "alpha": round(self.alpha, 2),
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "age_s": round(time.time() - self.last_updated, 1) if self.last_updated else None,
            "reasoning": self.reasoning[:200] if self.reasoning else "",
        }

    def needs_refresh(self, now: float | None = None) -> bool:
        if self.last_updated <= 0:
            return True
        now = now or time.time()
        return (now - self.last_updated) > ALPHA_REFRESH_SECONDS

    def should_bias_quoting(self) -> bool:
        """True if the signal is strong enough to bias market-making quotes."""
        return (
            self.alpha >= ALPHA_BIAS_THRESHOLD
            and self.direction != self.BIAS_NONE
            and self.confidence > 0.3
        )

    def should_go_directional(self) -> bool:
        """True if the mispricing is so large we should take a position."""
        return (
            self.alpha >= ALPHA_DIRECTIONAL_THRESHOLD
            and self.direction != self.BIAS_NONE
            and self.confidence > 0.5
        )

    def size_multiplier(self) -> float:
        """How much to scale position size based on alpha."""
        if self.alpha < ALPHA_BIAS_THRESHOLD:
            return 1.0  # no bias
        if self.alpha < ALPHA_DIRECTIONAL_THRESHOLD:
            return min(2.0, 1.0 + (self.alpha - ALPHA_BIAS_THRESHOLD) * 0.2)
        if self.alpha < ALPHA_EXTREME_THRESHOLD:
            return min(4.0, 2.0 + (self.alpha - ALPHA_DIRECTIONAL_THRESHOLD) * 0.3)
        return min(6.0, 4.0 + (self.alpha - ALPHA_EXTREME_THRESHOLD) * 0.2)


# ── Prompt for DeepSeek ───────────────────────────────────────────────

RESOLUTION_PROMPT_TEMPLATE = (
    "Estimate the true probability of this event resolving to YES. "
    "Do NOT output the current market price. Output ONLY the probability "
    "you believe is correct, plus a brief reasoning and confidence.\n\n"
    "Market: {question}\n"
    "Current market price of YES: {price}\n"
    "Reward pool: ${reward_rate}/day\n"
    "Total volume: ${volume}\n\n"
    "Respond as JSON: "
    '{{"estimated_probability": 0.XX, "confidence": 0.XX, "reasoning": "..."}}'
)


# ── Engine integration helper ─────────────────────────────────────────


def compute_alpha(
    market_price: float,
    estimated_probability: float,
    tick_size: float = 0.001,
) -> float:
    """|market_price - P(event)| / tick_size — the mispricing metric.

    A value of 3 means the market is 3 ticks (0.3 cents) away from
    the estimated true probability. A value of 30 means a 3-cent gap
    — massive mispricing.
    """
    if tick_size <= 0:
        return 0.0
    return abs(market_price - estimated_probability) / tick_size


def direction_from_prices(market_price: float, estimated_p: float) -> str:
    """Determine which side is mispriced.

    market_price < P(event) → YES is underpriced → BUY_YES
    market_price > P(event) → YES is overpriced → SELL_YES
    """
    diff = estimated_p - market_price
    if abs(diff) < 0.002:
        return ResolutionSignal.BIAS_NONE
    if diff > 0:
        return ResolutionSignal.BIAS_BUY_YES
    return ResolutionSignal.BIAS_SELL_YES


def build_resolution_prompt(
    question: str,
    price: float,
    reward_rate: float = 0.0,
    volume: float = 0.0,
) -> str:
    """Build the prompt for DeepSeek to estimate P(event)."""
    return RESOLUTION_PROMPT_TEMPLATE.format(
        question=question or "Unknown event",
        price=round(price, 4),
        reward_rate=round(reward_rate, 2),
        volume=round(volume, 0),
    )


async def estimate_resolution_probability(
    agent: Any,  # DeepSeekAgent
    question: str,
    market_price: float,
    *,
    reward_rate: float = 0.0,
    volume: float = 0.0,
    calibrator: Any | None = None,  # ResolutionCalibrator
) -> ResolutionSignal:
    """Ask DeepSeek for P(event), compute alpha, return the signal.

    When a calibrator is provided with >= 10 records, the raw LLM
    estimate is bias-corrected via Platt scaling before computing alpha.

    Returns a ResolutionSignal that the engine can use to bias quoting
    or take directional positions.
    """
    prompt = build_resolution_prompt(question, market_price, reward_rate, volume)
    try:
        result = await agent.chat_json_tool(
            messages=[{"role": "user", "content": prompt}],
            tool_name="probability_estimate",
            tool_schema={
                "type": "object",
                "properties": {
                    "estimated_probability": {"type": "number"},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["estimated_probability", "confidence", "reasoning"],
            },
            kind="resolution",
            description="Estimate true probability of an event resolving to YES",
        )
        # Raw agents return (parsed, resp); the governed wrapper returns a
        # single GovernedResponse whose agent_response holds the tool call.
        if isinstance(result, tuple):
            parsed, resp = result
        else:
            resp = getattr(result, "agent_response", None)
            parsed = {}
            for tc in getattr(resp, "tool_calls", []) or []:
                if isinstance(getattr(tc, "arguments", None), dict):
                    parsed = tc.arguments
                    break
            if not parsed:
                import json as _json

                try:
                    obj = _json.loads(getattr(resp, "content", "") or "")
                    if isinstance(obj, dict):
                        parsed = obj
                except (_json.JSONDecodeError, TypeError):
                    pass
        raw_p = float(parsed.get("estimated_probability", market_price))
        conf = float(parsed.get("confidence", 0.3))
        reasoning = str(parsed.get("reasoning", ""))
    except Exception:
        log.warning("resolution_probability_failed — using market price as estimate")
        return ResolutionSignal(
            condition_id="",
            question=question,
            market_price=market_price,
            estimated_probability=market_price,
            alpha=0.0,
            direction=ResolutionSignal.BIAS_NONE,
            confidence=0.0,
            last_updated=time.time(),
            reasoning="fallback_no_llm",
        )

    raw_p = max(0.01, min(0.99, raw_p))
    conf = max(0.0, min(1.0, conf))

    # Apply Platt-scaled calibration correction if available
    if calibrator is not None and hasattr(calibrator, "calibrated_p"):
        est_p = calibrator.calibrated_p(raw_p)
        cal_info = calibrator.summary()
        if cal_info["n_records"] >= 10:
            reasoning = f"[calib:α={cal_info['alpha']},β={cal_info['beta']},ece={cal_info['ece_pct']}] {reasoning}"
    else:
        est_p = raw_p

    alpha = compute_alpha(market_price, est_p)
    direction = direction_from_prices(market_price, est_p)

    return ResolutionSignal(
        condition_id="",
        question=question,
        market_price=market_price,
        estimated_probability=est_p,
        alpha=alpha,
        direction=direction,
        confidence=conf,
        last_updated=time.time(),
        reasoning=reasoning,
        raw_response=str(parsed),
    )
