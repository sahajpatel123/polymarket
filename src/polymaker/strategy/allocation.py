"""Risk-aware capital allocation across multiple markets.

Allocates a total bankroll across N markets using a risk-parity approach:
each market gets a share of capital proportional to its expected return
and inversely proportional to its risk (volatility).

The allocation is computed using a simplified risk-parity formula:
    weight_i = expected_return_i / risk_i^2

Where:
    expected_return_i = reward_daily_rate_i + rebate_potential_i
    risk_i = sigma_i (per-second volatility, annualized)

After computing raw weights, we normalize them to sum to 1.0 and apply
a maximum concentration limit (no market can have more than X% of capital).

This is more accurate than equal-weight allocation because it:
1. Rewards high-return markets
2. Penalizes high-risk markets
3. Provides natural diversification
4. Adapts to changing market conditions

Pure functions only — no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarketAllocation:
    """Allocation for one market."""
    condition_id: str
    weight: float          # fraction of total capital (0 to 1)
    expected_return: float  # expected daily return in USDC
    risk: float            # annualized volatility (risk measure)
    capital_usdc: float    # allocated capital in USDC


@dataclass(frozen=True, slots=True)
class AllocationInputs:
    """Inputs to the capital allocator."""
    markets: tuple[tuple[str, float, float], ...]  # (condition_id, expected_return, risk)
    total_capital_usdc: float
    max_concentration: float = 0.4  # max 40% in any single market
    min_allocation: float = 0.01    # min 1% to be included


@dataclass(frozen=True, slots=True)
class AllocationOutput:
    """Output of the capital allocator."""
    allocations: tuple[MarketAllocation, ...]
    total_allocated_usdc: float
    unallocated_usdc: float


def allocate_capital(inp: AllocationInputs) -> AllocationOutput:
    """Allocate capital across markets using risk-parity weights.

    weight_i = expected_return_i / risk_i^2

    Markets with zero or negative expected return get zero allocation.
    Markets with zero risk get a small default allocation.
    """
    if inp.total_capital_usdc <= 0 or not inp.markets:
        return AllocationOutput(allocations=(), total_allocated_usdc=0.0, unallocated_usdc=0.0)

    # Compute raw weights: expected_return / risk^2
    raw_weights: list[tuple[str, float, float, float]] = []
    total_weight = 0.0
    for cid, exp_return, risk in inp.markets:
        if exp_return <= 0 or risk <= 0:
            # Skip markets with no edge or no measurable risk
            continue
        weight = exp_return / (risk * risk)
        raw_weights.append((cid, weight, exp_return, risk))
        total_weight += weight

    if total_weight <= 0:
        return AllocationOutput(allocations=(), total_allocated_usdc=0.0,
                                unallocated_usdc=inp.total_capital_usdc)

    # Normalize weights to sum to 1.0
    normalized: list[tuple[str, float, float, float]] = []
    for cid, w, er, r in raw_weights:
        norm_w = w / total_weight
        normalized.append((cid, norm_w, er, r))

    # Filter by minimum allocation FIRST (before capping)
    above_min = [(cid, w, er, r) for cid, w, er, r in normalized if w >= inp.min_allocation]
    if not above_min:
        return AllocationOutput(allocations=(), total_allocated_usdc=0.0,
                                unallocated_usdc=inp.total_capital_usdc)

    # Re-normalize after filtering
    above_min_total = sum(w for _, w, _, _ in above_min)
    if above_min_total <= 0:
        return AllocationOutput(allocations=(), total_allocated_usdc=0.0,
                                unallocated_usdc=inp.total_capital_usdc)
    re_normalized = [(cid, w / above_min_total, er, r) for cid, w, er, r in above_min]

    # Apply max concentration cap (redistribute excess)
    capped: list[tuple[str, float, float, float]] = []
    excess = 0.0
    for cid, w, er, r in re_normalized:
        if w > inp.max_concentration:
            excess += w - inp.max_concentration
            capped.append((cid, inp.max_concentration, er, r))
        else:
            capped.append((cid, w, er, r))

    if excess > 0:
        # Redistribute excess to uncapped markets proportionally
        redist_pool = sum(w for _, w, _, _ in capped if w < inp.max_concentration)
        if redist_pool > 0:
            for i, (cid, w, er, r) in enumerate(capped):
                if w < inp.max_concentration:
                    share = w / redist_pool
                    new_w = w + excess * share
                    capped[i] = (cid, min(new_w, inp.max_concentration), er, r)

    # Build final allocations
    final: list[MarketAllocation] = []
    total_allocated = 0.0
    for cid, w, er, r in capped:
        capital = w * inp.total_capital_usdc
        final.append(MarketAllocation(
            condition_id=cid,
            weight=round(w, 6),
            expected_return=er,
            risk=r,
            capital_usdc=round(capital, 2),
        ))
        total_allocated += capital

    return AllocationOutput(
        allocations=tuple(final),
        total_allocated_usdc=round(total_allocated, 2),
        unallocated_usdc=round(inp.total_capital_usdc - total_allocated, 2),
    )


def annualize_vol(per_second_vol: float) -> float:
    """Convert per-second volatility to annualized volatility.

    Assumes 24/7 trading (crypto/DeFi assumption). For traditional markets,
    multiply by sqrt(252 * 6.5 * 3600) instead.
    """
    if per_second_vol <= 0:
        return 0.0
    seconds_per_year = 365.25 * 24.0 * 3600.0
    return per_second_vol * math.sqrt(seconds_per_year)
