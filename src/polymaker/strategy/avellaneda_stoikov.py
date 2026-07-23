"""Avellaneda-Stoikov optimal market-making model.

Reference: Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book"

The model provides optimal bid/ask prices for a market maker, accounting for:
- Inventory position (skew the quotes to reduce inventory)
- Volatility (widen the spread in volatile markets)
- Time horizon (tighten the spread as the horizon shortens)
- Order arrival rate (account for the probability of fills)

Reservation price (mid of optimal quotes):
    r(s, q, t) = s - q * gamma * sigma^2 * (T - t)

Optimal half-spread:
    delta = gamma * sigma^2 * (T - t) + (2 / gamma) * ln(1 + gamma / kappa)

Where:
    s = current mid price
    q = inventory (positive = long, negative = short)
    gamma = risk aversion parameter (higher = more risk averse)
    sigma = volatility
    T - t = time horizon remaining (in years, for consistency with sigma)
    kappa = order arrival rate parameter (higher = more fills expected)

The optimal bid and ask prices are:
    bid = r - delta / 2
    ask = r + delta / 2

This is more accurate than a simple linear skew because it:
1. Accounts for the time horizon (tightens as T approaches)
2. Accounts for the order arrival rate (adapts to market liquidity)
3. Provides a theoretically optimal spread (not just an empirical one)

Pure functions only — no I/O. Time-decayed parameters use the same Ewma
infrastructure as the rest of the strategy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ASInputs:
    """Inputs to the Avellaneda-Stoikov model.

    All quantities are in consistent units. For sigma (volatility), use the
    per-second realized vol; for T (time horizon), use seconds remaining.
    The product sigma^2 * (T - t) is then dimensionless.
    """
    mid: float           # current mid price (s)
    inventory: float     # inventory in shares (positive = long, negative = short)
    sigma: float         # per-second volatility (sigma_short from estimators)
    time_horizon_s: float  # time horizon remaining in seconds (T - t)
    gamma: float         # risk aversion parameter (typical: 0.01 to 1.0)
    kappa: float         # order arrival rate parameter (typical: 1.0 to 100.0)


@dataclass(frozen=True, slots=True)
class ASOutput:
    """Output of the Avellaneda-Stoikov model.

    The reservation price is the indifference price (the price at which the
    market maker is indifferent to trading). The optimal bid and ask are
    centered on the reservation price, separated by the optimal spread.
    """
    reservation: float   # indifference price (r)
    half_spread: float   # optimal half-spread (delta / 2)
    bid: float           # optimal bid price
    ask: float           # optimal ask price
    skew: float          # inventory skew (q * gamma * sigma^2 * T)


def avellaneda_stoikov(inp: ASInputs) -> ASOutput:
    """Compute optimal bid/ask prices using the Avellaneda-Stoikov model.

    The reservation price is skewed by inventory to reduce position:
        r = s - q * gamma * sigma^2 * T

    The optimal half-spread accounts for volatility, time, and arrival rate:
        delta = gamma * sigma^2 * T + (2 / gamma) * ln(1 + gamma / kappa)

    Returns the reservation price, half-spread, and optimal bid/ask.
    """
    if inp.time_horizon_s <= 0 or inp.sigma <= 0 or inp.gamma <= 0 or inp.kappa <= 0:
        # Degenerate inputs — return a simple mid quote with zero spread
        return ASOutput(
            reservation=inp.mid,
            half_spread=0.0,
            bid=inp.mid,
            ask=inp.mid,
            skew=0.0,
        )

    sigma_sq = inp.sigma * inp.sigma
    T = inp.time_horizon_s

    # Reservation price: skew by inventory to reduce position
    skew = inp.inventory * inp.gamma * sigma_sq * T
    reservation = inp.mid - skew

    # Optimal half-spread: volatility term + order arrival term
    vol_term = inp.gamma * sigma_sq * T
    arrival_term = (2.0 / inp.gamma) * math.log(1.0 + inp.gamma / inp.kappa)
    half_spread = 0.5 * (vol_term + arrival_term)

    bid = reservation - half_spread
    ask = reservation + half_spread

    return ASOutput(
        reservation=reservation,
        half_spread=half_spread,
        bid=bid,
        ask=ask,
        skew=skew,
    )


def gamma_from_profile(risk_aversion: float) -> float:
    """Convert a profile-level risk aversion to the AS gamma parameter.

    The AS gamma is typically much smaller than 1 (e.g., 0.01 to 1.0).
    A higher risk_aversion means a smaller gamma (more risk averse).
    """
    if risk_aversion <= 0:
        return 0.1  # default
    return 1.0 / (1.0 + risk_aversion * 10.0)


def kappa_from_liquidity(liquidity_usdc: float) -> float:
    """Estimate the order arrival rate parameter from market liquidity.

    Higher liquidity markets have higher arrival rates (kappa).
    Typical values: 1.0 (thin) to 100.0 (deep).
    """
    if liquidity_usdc <= 0:
        return 1.0
    # Scale: $1000 liquidity -> kappa ~10, $10000 -> kappa ~50, $100000 -> kappa ~100
    return min(100.0, max(1.0, math.sqrt(liquidity_usdc / 10.0)))
