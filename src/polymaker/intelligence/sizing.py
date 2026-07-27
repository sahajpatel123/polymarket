"""Percent-based per-trade sizing.

Replaces fixed ``base_size_usdc`` and ``q_max_usdc`` per profile with
fractions of the *current market allocation*. Every quote is sized
in % terms so the same profile behaves correctly across $30 and
$30,000 bankrolls.

Sizing rules (all % of the per-market allocation, not of total capital):
- per_order_pct      — how much of the allocation can a single order consume
- max_one_side_pct   — max % of allocation held on one side
- layers             — how many price levels to stack on each side
- per_trade_loss_pct — tight stop on a single fill (mirrors policy)
- fv_distance        — the FV→quote price gap; loss grows with it

The math is intentionally trivial: percent × allocation ÷ price, rounded
to the exchange's min order size and the market's reward-eligible size.

The output is :class:`SizingDecision` — what to quote, at what size, with
the derived loss-budget so the risk manager can refuse a quote that
exceeds the per-trade loss cap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ── Defaults (overridable per-profile) ────────────────────────────────

DEFAULT_PER_ORDER_PCT = 0.30          # 30% of allocation per single order
DEFAULT_MAX_ONE_SIDE_PCT = 0.60       # 60% of allocation on one side max
DEFAULT_LAYERS = 2                    # 2 price levels per side by default
DEFAULT_PER_TRADE_LOSS_PCT = 0.005    # 0.5% of capital per single fill
DEFAULT_LAYER_DECAY = 0.5             # each deeper layer is 50% the size


@dataclass(frozen=True)
class SizingParams:
    """Per-profile sizing parameters, all in % of the per-market allocation."""

    per_order_pct: float = DEFAULT_PER_ORDER_PCT
    max_one_side_pct: float = DEFAULT_MAX_ONE_SIDE_PCT
    layers: int = DEFAULT_LAYERS
    per_trade_loss_pct: float = DEFAULT_PER_TRADE_LOSS_PCT
    layer_decay: float = DEFAULT_LAYER_DECAY

    def __post_init__(self) -> None:
        if not (0.0 < self.per_order_pct <= 1.0):
            raise ValueError(f"per_order_pct must be in (0, 1]; got {self.per_order_pct}")
        if not (0.0 < self.max_one_side_pct <= 1.0):
            raise ValueError(f"max_one_side_pct must be in (0, 1]; got {self.max_one_side_pct}")
        if self.layers < 1:
            raise ValueError(f"layers must be >= 1; got {self.layers}")
        if not (0.0 < self.per_trade_loss_pct <= 0.05):
            raise ValueError(
                f"per_trade_loss_pct must be in (0, 0.05]; got {self.per_trade_loss_pct}"
            )
        if not (0.0 < self.layer_decay <= 1.0):
            raise ValueError(f"layer_decay must be in (0, 1]; got {self.layer_decay}")


@dataclass(frozen=True)
class SizingDecision:
    """The output of :func:`size_layers`.

    Sizes are in *shares* (not USDC) because that's what the exchange
    accepts. The caller converts to USDC when checking against risk caps.
    """

    side: str                # "BUY_YES" | "BUY_NO" | "SELL_YES" | "SELL_NO"
    layers: tuple[tuple[float, int], ...]   # ((price, size_shares), ...) per level
    per_trade_loss_usdc: float
    total_notional_usdc: float
    capped: bool             # True if any layer was clamped to exchange/reward min


def size_layers(
    side: str,
    fair_value: float,
    quote_price: float,
    market_allocation_usdc: float,
    params: SizingParams,
    *,
    exchange_min_shares: float = 5.0,
    reward_min_shares: float = 200.0,
    tick: float = 0.001,
) -> SizingDecision:
    """Compute a stack of limit orders for one side of one market.

    Parameters
    ----------
    side
        One of ``"BUY_YES"``, ``"BUY_NO"``, ``"SELL_YES"``, ``"SELL_NO"``.
    fair_value
        Current strategy fair value (used only to compute
        ``per_trade_loss_usdc``; the price levels themselves are
        determined by ``quote_price`` + tick steps).
    quote_price
        The price of the *first* (best) layer. Deeper layers stair-step
        away by ``tick``.
    market_allocation_usdc
        Total USDC allocated to this market (the per-market cap from
        policy, not the total bankroll).
    params
        Sizing parameters (per-order %, layers, etc.).
    exchange_min_shares, reward_min_shares, tick
        Exchange / market constraints. We round up to the nearest
        multiple of these.
    """
    if market_allocation_usdc <= 0:
        return SizingDecision(
            side=side, layers=(), per_trade_loss_usdc=0.0,
            total_notional_usdc=0.0, capped=False,
        )
    if quote_price <= 0 or fair_value <= 0:
        return SizingDecision(
            side=side, layers=(), per_trade_loss_usdc=0.0,
            total_notional_usdc=0.0, capped=False,
        )

    # Per-layer USDC budget, decaying for deeper layers.
    per_order_usdc = market_allocation_usdc * params.per_order_pct
    layer_usdcs = [per_order_usdc * (params.layer_decay ** i) for i in range(params.layers)]

    # Price stair-step away from quote_price by tick.
    is_buy = side.startswith("BUY")
    levels: list[tuple[float, int]] = []
    total_notional = 0.0
    capped = False

    for i, layer_usdc in enumerate(layer_usdcs):
        price = quote_price - (tick * i) if is_buy else quote_price + (tick * i)
        if price <= 0 or price >= 1.0:
            capped = True
            continue
        size_shares_f = layer_usdc / price
        size_shares = _round_up_to(size_shares_f, exchange_min_shares)
        if size_shares < reward_min_shares and reward_min_shares > 0:
            needed_usdc = reward_min_shares * price
            if needed_usdc <= layer_usdc * 1.5:
                size_shares = int(math.ceil(reward_min_shares))
            else:
                capped = True
        levels.append((price, size_shares))
        total_notional += size_shares * price

    per_trade_loss_usdc = (
        params.per_trade_loss_pct * market_allocation_usdc
    )
    if per_order_usdc > per_trade_loss_usdc and per_trade_loss_usdc > 0:
        scale = per_trade_loss_usdc / per_order_usdc
        scaled_levels = []
        for price, size_shares in levels:
            scaled_levels.append((price, max(0, int(size_shares * scale))))
        levels = scaled_levels
        total_notional = sum(p * s for p, s in scaled_levels)
        capped = True

    return SizingDecision(
        side=side,
        layers=tuple(levels),
        per_trade_loss_usdc=per_trade_loss_usdc,
        total_notional_usdc=total_notional,
        capped=capped,
    )


def allocation_from_confidence(
    capital_usdc: float,
    confidence: float,
    expected_reward_per_day_usdc: float,
    *,
    max_per_market_pct: float,
    min_reward_pct_per_day: float,
    min_allocation_usdc: float = 5.0,
) -> float:
    """Convert (LLM confidence, expected reward) → USDC allocation."""
    if capital_usdc <= 0 or confidence <= 0 or expected_reward_per_day_usdc <= 0:
        return 0.0

    reward_pct = expected_reward_per_day_usdc / capital_usdc
    if reward_pct < min_reward_pct_per_day:
        return 0.0

    seven_day = expected_reward_per_day_usdc * 7.0 * confidence
    raw = seven_day * 3.0
    capped = min(raw, capital_usdc * max_per_market_pct)
    if capped < min_allocation_usdc:
        return 0.0
    return capped


def _round_up_to(value: float, step: float) -> int:
    """Round ``value`` up to the nearest multiple of ``step``, as int."""
    if step <= 0:
        return int(math.ceil(value))
    return int(math.ceil(value / step) * step)
