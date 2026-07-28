"""Market attractiveness scoring for the scanner.

Profit-oriented ranking: reward density and rebate potential vs adverse-
selection risk (extremity, wide band, thin books). Higher score = more
attractive to make. Pure functions over MarketMeta.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.domain import MarketMeta


@dataclass(frozen=True, slots=True)
class MarketScore:
    condition_id: str
    reward_density: float  # est. reward $/day per $100 of two-sided liquidity
    rebate_potential: float  # est. daily rebate $ available to makers
    spread: float
    extremity: float  # 0 = mid ~0.5 (good), 1 = near 0/1 (bad payoff asymmetry)
    score: float
    # Profitability extras (optional consumers; score remains primary rank key)
    profit_score: float = 0.0  # bankroll-normalized expected daily return signal
    as_risk: float = 0.0  # 0..1 adverse-selection risk proxy
    # Dominator KPI: share-adjusted expected $/day (headline); monopoly is diagnostic only
    share_adjusted_expected_usdc: float = 0.0
    estimated_share_of_pool: float = 0.0
    monopoly_diagnostic_usdc: float = 0.0
    capital_skip: bool = False


def _mid(m: MarketMeta) -> float:
    if m.best_bid > 0 and m.best_ask > 0:
        return (m.best_bid + m.best_ask) / 2.0
    return 0.5


def reward_density(m: MarketMeta, quote_size_usdc: float = 100.0) -> float:
    """Rough reward $/day if we hold ~quote_size two-sided in-band.

    The exact per-order S((v-s)/v)^2 scoring depends on live competition; for
    ranking we use daily_rate scaled by how much of the (small) market our
    typical size represents, capped. This mirrors v1's gm_reward_per_100 as a
    relative ranking signal, not an absolute forecast.
    """
    if m.rewards_daily_rate <= 0 or m.rewards_max_spread <= 0:
        return 0.0
    liq = max(m.liquidity_num, quote_size_usdc)
    our_share = min(1.0, quote_size_usdc / liq)
    return m.rewards_daily_rate * our_share


def rebate_potential(m: MarketMeta) -> float:
    """Estimated daily maker-rebate POOL for the market, using the exact V2 fee
    formula (per-market rate + rebate rate, no hardcoding).

    Per-share taker fee = fee_rate * p*(1-p)  (py_clob_client_v2/fees.py).
    Daily taker shares ~ vol_24h / mid, so:
        daily fees   = (vol/mid) * fee_rate * mid*(1-mid) = vol * fee_rate * (1-mid)
        rebate pool  = daily fees * rebate_rate
    This is the whole-market pool; your take is (your maker-fill share) x pool.
    It's a trailing-volume estimate — actual depends on future flow + fill share.
    """
    if not m.fees_enabled or m.rebate_rate <= 0 or m.taker_fee_bps <= 0:
        return 0.0
    vol24 = m.volume_24hr
    if vol24 <= 0:
        return 0.0
    fee_rate = m.taker_fee_bps / 10000.0
    mid = _mid(m)
    daily_fees = vol24 * fee_rate * (1.0 - mid)
    return round(daily_fees * m.rebate_rate, 2)


def extremity(m: MarketMeta) -> float:
    """0 near 0.5 (balanced), ->1 near the 0/1 boundary (skip these)."""
    mid = _mid(m)
    return min(1.0, abs(mid - 0.5) / 0.5)


def adverse_selection_risk(m: MarketMeta) -> float:
    """0..1 proxy for toxic-fill risk: thin books, wide reward bands, extreme mids.

    Used to down-rank markets that pay high rewards only because they are
    hard to exit after an adverse print (Romania-style gap risk).
    """
    ext = extremity(m)
    # Wide reward band (cents) → more room to get picked off while "in band"
    band = max(0.0, float(m.rewards_max_spread or 0.0))
    band_risk = min(1.0, band / 10.0)  # 10c band → full risk
    # Thin liquidity → higher gap risk
    liq = max(float(m.liquidity_num or 0.0), 0.0)
    thin = 1.0 - min(1.0, liq / 20000.0)
    return min(1.0, 0.45 * ext + 0.30 * band_risk + 0.25 * thin)


def score_market(m: MarketMeta, *, bankroll_usdc: float = 100.0) -> MarketScore:
    """Rank markets by **share-adjusted expected income** at this bankroll.

    Primary rank key is share-adjusted expected $/day (pool × estimated maker
    share × uptime), not monopoly pool size. Fat pools with dense competition
    lose to thinner books we can dominate with the same capital.

    Monopoly diagnostic is stored for operators; it is never the sort key.
    Capital-ineligible markets (cannot fund rewardsMinSize two-sided) score 0.
    """
    from polymaker.strategy.share_planning import plan_share_adjusted

    mid = _mid(m)
    plan = plan_share_adjusted(
        bankroll_usdc=float(bankroll_usdc),
        rewards_daily_rate=float(m.rewards_daily_rate or 0.0),
        rewards_min_size=float(m.rewards_min_size or 0.0),
        market_liquidity=float(m.liquidity_num or 0.0),
        typical_price=mid,
        exchange_min_shares=float(m.min_order_size or 5.0),
        condition_id=m.condition_id,
    )
    quote_ref = max(plan.quote_size_usdc, max(10.0, min(200.0, bankroll_usdc * 0.15)))
    rd = reward_density(m, quote_size_usdc=quote_ref)
    # Prefer share-adjusted plan density when eligible
    if plan.eligible and plan.share_adjusted_expected_usdc > 0:
        rd = plan.share_adjusted_expected_usdc
    rp = rebate_potential(m)
    ext = extremity(m)
    as_risk = adverse_selection_risk(m)
    spread = max(0.0, m.best_ask - m.best_bid) if (m.best_bid and m.best_ask) else 1.0

    our_share = plan.estimated_share_of_pool if plan.eligible else 0.0
    # HEADLINE rank key = share-adjusted expected $ (dominator thesis).
    # Rank tracks selection_score closely: pool×share×uptime with mild AS/ext
    # haircuts. Absolute liquidity must NOT overturn a higher share-adjusted
    # expectation (that was the monopoly-pool trap).
    income = plan.share_adjusted_expected_usdc + 0.10 * rp * our_share
    penalty = (1.0 - 0.35 * ext) * (1.0 / (1.0 + spread * 12.0)) * (1.0 - 0.40 * as_risk)
    # Share bonus: reward markets we can actually dominate
    share_boost = 1.0 + min(0.5, our_share)  # up to +50% when share→0.35+
    raw = income * penalty * share_boost
    if plan.skip:
        raw = 0.0
    b = max(bankroll_usdc, 1.0)
    profit = plan.share_adjusted_expected_usdc * (1.0 - 0.5 * as_risk) / b

    return MarketScore(
        condition_id=m.condition_id,
        reward_density=round(rd, 3),
        rebate_potential=round(rp, 3),
        spread=round(spread, 4),
        extremity=round(ext, 3),
        score=round(raw, 4),
        profit_score=round(profit, 6),
        as_risk=round(as_risk, 4),
        share_adjusted_expected_usdc=round(plan.share_adjusted_expected_usdc, 6),
        estimated_share_of_pool=round(plan.estimated_share_of_pool, 6),
        monopoly_diagnostic_usdc=round(plan.monopoly_diagnostic_usdc, 6),
        capital_skip=bool(plan.skip),
    )
