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
    """Rank markets by expected maker income net of AS risk.

    Prefer high rewards_daily_rate with manageable competition and low AS risk
    over raw breadth. bankroll_usdc scales the assumed quote size so small
    accounts do not pretend they can own large pools.
    """
    quote_ref = max(10.0, min(200.0, bankroll_usdc * 0.15))
    rd = reward_density(m, quote_size_usdc=quote_ref)
    rp = rebate_potential(m)
    ext = extremity(m)
    as_risk = adverse_selection_risk(m)
    spread = max(0.0, m.best_ask - m.best_bid) if (m.best_bid and m.best_ask) else 1.0

    ref = max(quote_ref, 50.0)
    our_share = min(0.35, ref / max(m.liquidity_num, ref))
    # Income: rewards (share-adjusted) dominate; rebates secondary
    income = rd + 0.5 * rp * our_share
    # Stronger extremity + spread + AS penalties than v1 (profit over breadth)
    penalty = (1.0 - 0.55 * ext) * (1.0 / (1.0 + spread * 25.0)) * (1.0 - 0.6 * as_risk)
    # Viability: need real depth; raise floor so dust books never rank high
    viability = min(1.0, m.liquidity_num / 5000.0)
    # Reward-rate boost: markets with large absolute pools are more worth the
    # fixed cost of watching (scaled so $50/day is ~1.0, $300/day ~1.6)
    rate_boost = 1.0 + min(1.0, float(m.rewards_daily_rate or 0.0) / 300.0)
    raw = income * penalty * viability * rate_boost
    # profit_score ≈ expected daily $ / bankroll (signal, not guarantee)
    b = max(bankroll_usdc, 1.0)
    profit = (rd * (1.0 - 0.5 * as_risk) + 0.25 * rp * our_share) / b

    return MarketScore(
        condition_id=m.condition_id,
        reward_density=round(rd, 3),
        rebate_potential=round(rp, 3),
        spread=round(spread, 4),
        extremity=round(ext, 3),
        score=round(raw, 4),
        profit_score=round(profit, 6),
        as_risk=round(as_risk, 4),
    )
