"""Kelly-inspired optimal position sizing for market making.

The Kelly criterion provides the optimal fraction of capital to risk on a
series of independent bets, maximizing the long-term growth rate.

For a market maker, the "bet" is the inventory risk. The Kelly fraction
is adapted to account for:
- Edge (expected return per trade)
- Volatility (risk per trade)
- Inventory (current position)
- Bankroll (available capital)

The full Kelly criterion for a binary outcome:
    f* = (p * (b + 1) - 1) / b

Where:
    p = probability of winning
    b = net odds (payoff / stake)

For a market maker, we adapt this to:
    edge = expected PnL per share (mid - quote price for a buy)
    variance = sigma^2 * T (variance of PnL over the holding period)
    kelly_frac = edge / variance  (optimal fraction of capital to risk)

We then apply a "half-Kelly" or "quarter-Kelly" fraction for safety
(Kelly is aggressive and can lead to large drawdowns).

The final size is:
    size_shares = (kelly_frac * bankroll) / price  (capped by max_position)

This is more accurate than a fixed base_size because it:
1. Adapts to the actual edge (wider edge = larger size)
2. Adapts to volatility (higher vol = smaller size)
3. Adapts to inventory (reduce size as inventory grows)
4. Adapts to bankroll (scale with available capital)

Pure functions only — no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KellyInputs:
    """Inputs to the Kelly-inspired sizing model.

    All quantities are in consistent units (USDC for capital, shares for size,
    price in (0, 1), sigma in per-second units).
    """
    edge: float            # expected PnL per share (positive = edge for us)
    sigma: float           # per-second volatility of the asset
    time_horizon_s: float  # expected holding period in seconds
    bankroll_usdc: float   # available capital in USDC
    inventory_shares: float  # current inventory in shares
    max_inventory_shares: float  # maximum allowed inventory
    kelly_fraction: float = 0.25  # fraction of Kelly to use (0.25 = quarter-Kelly)
    price: float = 0.5     # current price (for converting USDC to shares)
    min_size_shares: float = 5.0  # exchange minimum order size


@dataclass(frozen=True, slots=True)
class KellyOutput:
    """Output of the Kelly-inspired sizing model.

    The recommended size is the number of shares to trade in the direction
    of the edge. The fraction is the fraction of bankroll being risked.
    """
    size_shares: float     # recommended number of shares
    fraction: float        # fraction of bankroll being risked
    edge_ratio: float      # edge / (sigma * sqrt(T)) — signal-to-noise ratio


def kelly_size(inp: KellyInputs) -> KellyOutput:
    """Compute optimal position size using Kelly-inspired logic.

    The edge/variance ratio determines the optimal fraction of capital to
    risk. We apply a safety fraction (default quarter-Kelly) to reduce
    drawdown risk. The size is capped by the maximum inventory limit.
    """
    if inp.bankroll_usdc <= 0 or inp.price <= 0 or inp.price >= 1:
        return KellyOutput(size_shares=0.0, fraction=0.0, edge_ratio=0.0)

    if inp.time_horizon_s <= 0 or inp.sigma <= 0:
        # No time or no vol — can't compute variance, use conservative sizing
        return KellyOutput(size_shares=0.0, fraction=0.0, edge_ratio=0.0)

    # Variance of PnL over the holding period (sigma^2 * T)
    variance = inp.sigma * inp.sigma * inp.time_horizon_s

    if variance <= 0:
        return KellyOutput(size_shares=0.0, fraction=0.0, edge_ratio=0.0)

    # Edge/variance ratio (Kelly-like fraction)
    raw_fraction = inp.edge / variance

    # Edge ratio for reporting (signal-to-noise: edge / (sigma * sqrt(T)))
    edge_ratio = inp.edge / (inp.sigma * math.sqrt(inp.time_horizon_s)) if inp.sigma > 0 else 0.0

    # Apply safety fraction (quarter-Kelly by default)
    safe_fraction = raw_fraction * inp.kelly_fraction

    # Clamp fraction to [-1, 1] (can't risk more than 100% of bankroll)
    safe_fraction = max(-1.0, min(1.0, safe_fraction))

    # Convert fraction to USDC
    position_usdc = safe_fraction * inp.bankroll_usdc

    # Convert USDC to shares
    size_shares = position_usdc / inp.price

    # Cap by maximum inventory (don't exceed the limit)
    if abs(size_shares) > inp.max_inventory_shares:
        size_shares = math.copysign(inp.max_inventory_shares, size_shares)

    # Reduce size if we already have inventory in the same direction
    # (avoid adding to an already-large position)
    if inp.inventory_shares * size_shares > 0:
        # Same direction: reduce by the fraction of inventory already held
        inventory_frac = abs(inp.inventory_shares) / max(inp.max_inventory_shares, 1.0)
        size_shares *= max(0.0, 1.0 - inventory_frac)

    # Apply minimum size threshold (below this, don't trade)
    if abs(size_shares) < inp.min_size_shares:
        size_shares = 0.0

    return KellyOutput(
        size_shares=round(size_shares, 2),
        fraction=round(safe_fraction, 6),
        edge_ratio=round(edge_ratio, 4),
    )


def edge_from_spread(half_spread: float, tick: float) -> float:
    """Compute the expected edge per share from a half-spread.

    For a market maker, the edge per share is approximately the half-spread
    (we earn half the spread on each round-trip). The tick size sets the
    minimum edge.
    """
    return max(half_spread, tick)


def time_horizon_from_liquidity(liquidity_usdc: float) -> float:
    """Estimate the expected holding period from market liquidity.

    Higher liquidity markets have shorter holding periods (faster fills).
    Typical values: 60s (very deep) to 3600s (thin).
    """
    if liquidity_usdc <= 0:
        return 3600.0
    # Scale: $1000 -> 3600s, $10000 -> 600s, $100000 -> 60s
    return max(60.0, min(3600.0, 3600.0 / math.sqrt(liquidity_usdc / 1000.0)))
