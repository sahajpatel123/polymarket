"""Unified advanced quoting: Avellaneda-Stoikov pricing + Kelly sizing.

Combines the optimal market-making model (Avellaneda-Stoikov) with the
optimal position sizing model (Kelly-inspired) to produce quotes that
account for inventory, volatility, toxicity, regime, edge, and bankroll.

Production path (when ``StrategyProfile.use_advanced_quoting`` is True).
Falls back to empty targets for EVENT/HALTED; REDUCE_ONLY suppresses
new inventory (entries only — exits still go through construct_quotes
in the engine when needed).

Reference:
- Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book"
- Kelly (1956), "A new interpretation of information rate"
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Regime
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.avellaneda_stoikov import (
    ASInputs,
    avellaneda_stoikov,
    gamma_from_profile,
    kappa_from_liquidity,
)
from polymaker.strategy.edge import clamp_to_reward_band, half_spread_floor
from polymaker.strategy.kelly import (
    KellyInputs,
    edge_from_spread,
    kelly_size,
    time_horizon_from_liquidity,
)


@dataclass(frozen=True, slots=True)
class AdvancedQuoteInputs:
    """Inputs to the advanced quoting model."""

    meta: MarketMeta
    fv: float  # fair value (YES)
    sigma: float  # per-second volatility
    yes_view: BookView
    no_view: BookView
    pos_yes: Position
    pos_no: Position
    profile: StrategyProfile
    bankroll_usdc: float  # available capital
    now: float
    # Production controls (parity with construct_quotes)
    regime: Regime = Regime.QUIET
    toxicity: float = 0.0
    risk_size_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class AdvancedQuoteOutput:
    """Output of the advanced quoting model."""

    bid: float  # optimal YES bid price
    ask: float  # optimal YES ask price
    size_yes_shares: float  # recommended YES size
    size_no_shares: float  # recommended NO size
    reservation: float  # Avellaneda-Stoikov reservation price
    half_spread: float  # optimal half-spread
    edge_ratio: float  # signal-to-noise ratio


def compute_advanced_quotes(inp: AdvancedQuoteInputs) -> AdvancedQuoteOutput:
    """Compute optimal bid/ask and sizes using Avellaneda-Stoikov + Kelly.

    Production guards (beyond pure AS/Kelly):
    - EVENT / HALTED → zero sizes (engine cancels via empty targets)
    - REDUCE_ONLY → zero entry sizes (engine may still post exits via simple path)
    - Toxicity widens half-spread; TRENDING halves size; risk_size_scale applies
    - Soft inventory cap stops the adding side
    - Never bid above FV − min_edge_ticks; join best bid rather than jump
    - Size floored toward rewards_min_size * reward_size_mult when scoring
    """
    m = inp.meta
    p = inp.profile
    tick = m.tick_size
    dec = m.price_decimals

    empty = AdvancedQuoteOutput(
        bid=tick, ask=1.0 - tick,
        size_yes_shares=0.0, size_no_shares=0.0,
        reservation=inp.fv, half_spread=0.0, edge_ratio=0.0,
    )
    if inp.regime in (Regime.EVENT, Regime.HALTED):
        return empty
    if inp.regime is Regime.REDUCE_ONLY:
        # Entries suppressed; engine should use construct_quotes for exits.
        return empty

    net_shares = inp.pos_yes.size - inp.pos_no.size
    max_inv = p.q_max_usdc / max(inp.fv, tick)
    u = _clamp(net_shares / max_inv, -1.0, 1.0) if max_inv > 0 else 0.0

    T = time_horizon_from_liquidity(m.liquidity_num)
    gamma = gamma_from_profile(p.gamma)
    kappa = kappa_from_liquidity(m.liquidity_num)

    as_yes = avellaneda_stoikov(ASInputs(
        mid=inp.fv,
        inventory=net_shares,
        sigma=inp.sigma,
        time_horizon_s=T,
        gamma=gamma,
        kappa=kappa,
    ))

    # Economic floor (fee + AS) then AS/Kelly half-spread + toxicity, reward clamp
    econ_floor = half_spread_floor(
        m,
        fv=inp.fv,
        sigma=inp.sigma,
        toxicity=inp.toxicity,
        tick=tick,
        delta_min_ticks=p.delta_min_ticks,
    )
    reward_band = m.rewards_max_spread / 100.0
    if inp.regime is Regime.QUIET and reward_band > 0:
        floor = max(p.delta_min_ticks * tick, min(econ_floor, reward_band))
    else:
        floor = econ_floor
    half = max(as_yes.half_spread, floor)
    half = half + p.c_tox * max(0.0, inp.toxicity) * tick * 10.0
    half = clamp_to_reward_band(
        half,
        base=floor,
        reward_band=reward_band,
        quiet=(inp.regime is Regime.QUIET),
    )
    half = max(half, tick)

    # Reservation-anchored mid (skewed), then apply half-spread.
    # Anchor to FV so a wild AS reservation cannot walk us to 0.001.
    res = _clamp(as_yes.reservation, tick, 1.0 - tick)
    res = 0.7 * res + 0.3 * inp.fv  # blend: keep AS skew, stay near FV
    bid_yes = _round_to_tick(res - half, tick, dec, up=False)
    ask_yes = _round_to_tick(res + half, tick, dec, up=True)

    # Never bid above FV − min_edge
    edge_floor = inp.fv - p.min_edge_ticks * tick
    bid_yes = min(bid_yes, edge_floor)

    # Always keep entry bids inside the reward band when one exists — TRENDING
    # cuts size, not score eligibility. Dust best-bids (0.001) must not pull us
    # out of band (livecfg tape regression).
    if reward_band > 0:
        # leave 1 tick of headroom so abs(price-mid) <= band under float noise
        band_lo = inp.fv - reward_band + tick
        bid_yes = max(bid_yes, band_lo)
        bid_yes = min(bid_yes, edge_floor)

    # Join best bid only if it is still in-band / near our target
    if inp.yes_view.best_bid is not None and bid_yes > inp.yes_view.best_bid + 1e-12:
        bb = inp.yes_view.best_bid
        if reward_band <= 0 or abs(bb - inp.fv) <= reward_band + tick:
            bid_yes = bb
    # Never cross the ask
    if inp.yes_view.best_ask is not None and bid_yes >= inp.yes_view.best_ask - 1e-12:
        bid_yes = max(tick, inp.yes_view.best_ask - tick)

    # Re-apply band after join/cross adjustments
    if reward_band > 0:
        bid_yes = max(bid_yes, inp.fv - reward_band + tick)
        bid_yes = min(bid_yes, edge_floor)

    bid_yes = max(tick, min(bid_yes, 1.0 - tick))
    ask_yes = max(tick, min(ask_yes, 1.0 - tick))
    if bid_yes >= ask_yes:
        bid_yes = max(tick, ask_yes - tick)

    # NO bid is the complementary long-NO maker bid (USDC), near 1−FV.
    no_fv = 1.0 - inp.fv
    no_bid = _round_to_tick(no_fv - half, tick, dec, up=False)
    no_edge = no_fv - p.min_edge_ticks * tick
    no_bid = min(no_bid, no_edge)
    if reward_band > 0:
        no_bid = max(no_bid, no_fv - reward_band + tick)
        no_bid = min(no_bid, no_edge)
    if inp.no_view.best_bid is not None and no_bid > inp.no_view.best_bid + 1e-12:
        bb = inp.no_view.best_bid
        if reward_band <= 0 or abs(bb - no_fv) <= reward_band + tick:
            no_bid = bb
    if inp.no_view.best_ask is not None and no_bid >= inp.no_view.best_ask - 1e-12:
        no_bid = max(tick, inp.no_view.best_ask - tick)
    if reward_band > 0:
        no_bid = max(no_bid, no_fv - reward_band + tick)
        no_bid = min(no_bid, no_edge)
    no_bid = max(tick, min(no_bid, 1.0 - tick))

    # Engine derives NO price as 1−ask; set ask so 1−ask == no_bid.
    ask_yes = _round_to_tick(1.0 - no_bid, tick, dec, up=True)
    ask_yes = max(ask_yes, bid_yes + tick)

    edge = edge_from_spread(half, tick)
    bankroll = max(inp.bankroll_usdc, p.bankroll_usdc, p.q_max_usdc, 1.0)
    kf = float(getattr(p, "kelly_fraction", 0.25) or 0.25)
    kf = max(0.01, min(1.0, kf))

    kelly_yes = kelly_size(KellyInputs(
        edge=edge,
        sigma=max(inp.sigma, 1e-6),
        time_horizon_s=T,
        bankroll_usdc=bankroll * 0.5,
        inventory_shares=inp.pos_yes.size,
        max_inventory_shares=max_inv,
        kelly_fraction=kf,
        price=max(inp.fv, tick),
    ))
    kelly_no = kelly_size(KellyInputs(
        edge=edge,
        sigma=max(inp.sigma, 1e-6),
        time_horizon_s=T,
        bankroll_usdc=bankroll * 0.5,
        inventory_shares=inp.pos_no.size,
        max_inventory_shares=max_inv,
        kelly_fraction=kf,
        price=max(no_fv, tick),
    ))

    regime_scale = 0.35 if inp.regime is Regime.TRENDING else 1.0
    tox_scale = 1.0 / (1.0 + max(0.0, inp.toxicity) * 12.0)
    scale = regime_scale * tox_scale * _clamp(inp.risk_size_scale, 0.0, 1.0)

    size_yes = kelly_yes.size_shares * scale
    size_no = kelly_no.size_shares * scale

    # Soft inventory: stop adding on the heavy side
    soft = p.q_soft_frac
    if u >= soft:
        size_yes = 0.0
    if u <= -soft:
        size_no = 0.0

    # Reward min-size floor (shares) — only if we can afford notional
    reward_floor = m.rewards_min_size * p.reward_size_mult
    yes_notional_cap = max(p.base_size_usdc, p.q_max_usdc * 0.5) / max(bid_yes, tick)
    no_notional_cap = max(p.base_size_usdc, p.q_max_usdc * 0.5) / max(no_bid, tick)
    if size_yes > 0 and reward_floor > 0 and reward_floor <= yes_notional_cap:
        size_yes = max(size_yes, reward_floor)
    if size_no > 0 and reward_floor > 0 and reward_floor <= no_notional_cap:
        size_no = max(size_no, reward_floor)

    size_yes = min(size_yes, yes_notional_cap) if size_yes > 0 else 0.0
    size_no = min(size_no, no_notional_cap) if size_no > 0 else 0.0

    return AdvancedQuoteOutput(
        bid=bid_yes,
        ask=ask_yes,
        size_yes_shares=max(0.0, size_yes),
        size_no_shares=max(0.0, size_no),
        reservation=res,
        half_spread=half,
        edge_ratio=kelly_yes.edge_ratio,
    )


def _round_to_tick(price: float, tick: float, decimals: int, *, up: bool) -> float:
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, decimals)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
