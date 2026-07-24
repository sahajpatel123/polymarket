"""Maker edge math: fee/adverse-selection floors and expected daily return.

Pure helpers used by quoting, scoring, and backtest profitability reporting.
Goal: positive expected maker edge = rewards + rebates − adverse selection,
not reward uptime alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.domain import MarketMeta


def taker_fee_per_share(price: float, taker_fee_bps: float) -> float:
    """V2 per-share taker fee ≈ fee_rate · p · (1 − p)."""
    if taker_fee_bps <= 0 or price <= 0 or price >= 1:
        return 0.0
    rate = taker_fee_bps / 10000.0
    return rate * price * (1.0 - price)


def adverse_selection_buffer(
    *,
    sigma: float,
    toxicity: float,
    tick: float,
    hold_s: float = 60.0,
    tox_weight: float = 2.0,
) -> float:
    """Expected adverse move over a short hold after a toxic fill.

    σ is per-second vol of FV; scale by sqrt(hold) and toxicity intensity.
    Floor at one tick so thin quiet books still keep a buffer.
    """
    move = max(0.0, sigma) * math.sqrt(max(hold_s, 1.0))
    tox = max(0.0, toxicity)
    buf = move * (1.0 + tox_weight * tox)
    return max(tick, buf)


def half_spread_floor(
    meta: MarketMeta,
    *,
    fv: float,
    sigma: float,
    toxicity: float,
    tick: float,
    delta_min_ticks: int,
    as_hold_s: float = 45.0,
) -> float:
    """Minimum half-spread that covers fee + adverse selection + profile floor.

    For maker-only posts the direct fee is usually 0, but a fill that we later
    exit as taker pays fee, and toxic fills cost ~AS buffer. Both must sit
    inside δ so the quote is not negative-EV by construction.
    """
    base = max(1, delta_min_ticks) * tick
    # Worst-case mid fee when we take out of a bad fill near FV.
    fee = taker_fee_per_share(fv, float(meta.taker_fee_bps or 0.0))
    # Maker rebate recovers a fraction of the *other* side's fee when we fill;
    # we still need edge before rebate — use full fee as conservative floor.
    as_buf = adverse_selection_buffer(
        sigma=sigma, toxicity=toxicity, tick=tick, hold_s=as_hold_s,
    )
    # Half-spread needs to cover ~half of round-trip fee+AS on each side.
    economic = 0.5 * fee + as_buf
    return max(base, economic, tick)


def clamp_to_reward_band(
    delta: float, *, base: float, reward_band: float, quiet: bool,
) -> float:
    """In QUIET, keep δ inside the liquidity-rewards scoring band when possible."""
    if not quiet or reward_band <= 0:
        return max(delta, base)
    return min(max(delta, base), max(base, reward_band))


@dataclass(frozen=True, slots=True)
class DailyReturnEstimate:
    """Share-aware daily return estimate for profitability scoreboard."""

    bankroll_usdc: float
    runtime_hours: float
    spread_usdc: float
    reward_pool_accrual_usdc: float
    our_reward_share: float
    reward_our_usdc: float
    rebate_est_usdc: float
    total_est_usdc: float
    daily_return_pct: float  # fraction of bankroll per day (0.15 = 15%/day)
    gap_to_15pct: float  # how much daily_return_pct is below 0.15 (0 if met)
    target_band_hit: bool  # True if in [0.15, 0.25] or above 0.15 with risk intact


def competition_share(
    *,
    our_quote_usdc: float,
    market_liquidity: float,
    max_share: float = 0.35,
    n_competing_makers: float = 3.0,
    competitor_quote_usdc: float | None = None,
) -> float:
    """Estimate our share of the liquidity-reward pool.

    Liquidity rewards compete among *makers in the scoring band*, not against
    the entire L2 book (which can be millions of far-from-mid size). Model:

        share ≈ our / (our + (n-1) * competitor_size)

    with competitor_size defaulting to our size (symmetric makers). Full-book
    liquidity only tightens the share when it implies denser competition
    (liquidity / 50 as a soft competitor pool). Always capped at max_share.
    """
    if our_quote_usdc <= 0:
        return 0.0
    n = max(1.0, n_competing_makers)
    # Fixed competitor size (not mirrored to our size) so concentrating capital
    # actually increases share — critical for 15%+/day targeting on small books.
    default_comp = 40.0  # ~typical competing maker notional in-band
    comp = competitor_quote_usdc if competitor_quote_usdc is not None else default_comp
    liq_comp = max(0.0, float(market_liquidity or 0.0)) / 80.0
    others = max(comp * (n - 1.0), liq_comp * 0.2)
    denom = our_quote_usdc + others
    if denom <= 0:
        return 0.0
    return min(max_share, our_quote_usdc / denom)


def estimate_daily_return(
    *,
    bankroll_usdc: float,
    runtime_hours: float,
    spread_usdc: float,
    reward_pool_accrual_usdc: float,
    rebate_est_usdc: float,
    our_quote_usdc: float,
    market_liquidity: float,
    max_share: float = 0.35,
) -> DailyReturnEstimate:
    """Combine PnL components into a bankroll-normalized daily return fraction."""
    share = competition_share(
        our_quote_usdc=our_quote_usdc,
        market_liquidity=market_liquidity,
        max_share=max_share,
    )
    reward_our = reward_pool_accrual_usdc * share
    total = spread_usdc + reward_our + rebate_est_usdc
    b = max(bankroll_usdc, 1e-9)
    days = max(runtime_hours, 1e-6) / 24.0
    # annualize to per-day rate: total earned over window / bankroll / days
    daily = (total / b) / days if days > 0 else 0.0
    gap = max(0.0, 0.15 - daily)
    hit = daily >= 0.15
    return DailyReturnEstimate(
        bankroll_usdc=bankroll_usdc,
        runtime_hours=runtime_hours,
        spread_usdc=spread_usdc,
        reward_pool_accrual_usdc=reward_pool_accrual_usdc,
        our_reward_share=share,
        reward_our_usdc=reward_our,
        rebate_est_usdc=rebate_est_usdc,
        total_est_usdc=total,
        daily_return_pct=daily,
        gap_to_15pct=gap,
        target_band_hit=hit,
    )


def target_reward_capital(
    rewards_daily_rate: float,
    *,
    bankroll_usdc: float,
    target_daily_frac: float = 0.18,
    max_share: float = 0.35,
    assumed_uptime: float = 0.85,
) -> float:
    """Capital to rest on a market to approach target daily return from rewards.

    If pool * uptime * share / bankroll ≈ target, solve for share then capital
    under competition_share model with unknown liq → use share * bankroll as
    notional budget (caller caps by risk).
    """
    if rewards_daily_rate <= 0 or bankroll_usdc <= 0 or target_daily_frac <= 0:
        return 0.0
    # Needed our-reward $/day
    need = target_daily_frac * bankroll_usdc
    # Max we can extract from this pool at max_share
    max_extract = rewards_daily_rate * assumed_uptime * max_share
    if max_extract <= 0:
        return 0.0
    share = min(max_share, need / (rewards_daily_rate * assumed_uptime))
    # Map share → notional: share ≈ capital/liq unknown; allocate share * bankroll
    # (concentrated: put up to that fraction of bankroll on this market).
    return min(bankroll_usdc * max_share, bankroll_usdc * max(share, 0.05))
