"""Pure quote construction: (market state, inventory, params) -> TargetQuotes.

This is the deterministic core of the strategy. No I/O, no wall-clock reads
except values passed in. Everything here is exercised directly by unit tests.

Model (see the README):
  reservation  r  = FV - skew(inventory)
  half-spread  δ  = base + c_vol·σ + c_tox·toxicity   (clamped to reward band in QUIET)
  YES entry bid   = r - δ                       (BUY YES, USDC-collateralized)
  NO  entry bid   = (1 - r) - δ                  (BUY NO; implied YES ask at r + δ)
  exits           = SELL limits on held inventory, walked toward the touch by urgency

The BUY-YES + BUY-NO pair is the canonical two-sided quote: both are bids, both
score rewards, and a filled pair merges back to USDC at locked edge 1 - p - q.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Quote, Regime, Side, TargetQuotes
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.edge import clamp_to_reward_band, half_spread_floor

_EPS = 1e-9


def round_to_tick(price: float, tick: float, decimals: int, *, up: bool) -> float:
    """Snap a price to the tick grid, rounding up or down, clamped to (0,1)."""
    n = price / tick
    n = math.ceil(n - _EPS) if up else math.floor(n + _EPS)
    p = round(n * tick, decimals)
    return min(max(p, tick), 1.0 - tick)


def compute_fair_value(microprice: float, flow_z: float, tick: float, weight: float = 0.5) -> float:
    """Nudge the microprice by bounded signed flow. Clamped to (tick, 1-tick)."""
    fv = microprice + weight * flow_z * tick
    return min(max(fv, tick), 1.0 - tick)


@dataclass(frozen=True, slots=True)
class QuoteInputs:
    meta: MarketMeta
    regime: Regime
    fv: float  # YES fair value in (0,1)
    vol_short: float
    toxicity: float
    yes_view: BookView
    no_view: BookView
    pos_yes: Position
    pos_no: Position
    profile: StrategyProfile
    now: float
    risk_size_scale: float = 1.0  # RiskManager may throttle size in [0,1]
    yes_exit_urgency: float = 0.0  # [0,1]; engine raises with hold time / adverse drift
    no_exit_urgency: float = 0.0


def construct_quotes(inp: QuoteInputs) -> TargetQuotes:
    m = inp.meta
    p = inp.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    if inp.regime in (Regime.EVENT, Regime.HALTED):
        return TargetQuotes(cid, inp.regime, ())

    quotes: list[Quote] = []

    # ── inventory in YES-equivalent shares; holding NO is short YES ──────
    net_shares = inp.pos_yes.size - inp.pos_no.size
    q_max_shares = p.q_max_usdc / max(inp.fv, tick)
    u = _clamp(net_shares / q_max_shares, -1.0, 1.0) if q_max_shares > 0 else 0.0
    reward_floor = m.rewards_min_size * p.reward_size_mult  # scoring size w/ margin

    # Inventory skew: quadratic taper near |u|→1 so edge compounds without
    # over-skewing mid-range inventory (linear gamma·σ·u under-reacts at tails).
    skew = p.gamma * inp.vol_short * u * (1.0 + 0.5 * abs(u))

    # ── half-spread: fee/AS economic floor + vol/tox + reward-band clamp ─
    econ = half_spread_floor(
        m,
        fv=inp.fv,
        sigma=inp.vol_short,
        toxicity=inp.toxicity,
        tick=tick,
        delta_min_ticks=p.delta_min_ticks,
    )
    reward_band = m.rewards_max_spread / 100.0
    # QUIET farming: stay inside the reward band so we keep scoring; AS is
    # handled via size cuts (tox_scale), not by walking out of band.
    # Non-QUIET: use full economic floor (protect capital over rewards).
    if inp.regime == Regime.QUIET and reward_band > 0:
        base = max(p.delta_min_ticks * tick, min(econ, reward_band))
    else:
        base = econ
    delta = base + p.c_vol * inp.vol_short + p.c_tox * max(0.0, inp.toxicity)
    delta = clamp_to_reward_band(
        delta,
        base=base,
        reward_band=reward_band,
        quiet=(inp.regime == Regime.QUIET),
    )
    delta = max(delta, tick)

    r = inp.fv - skew
    yes_bid_target = r - delta
    no_bid_target = (1.0 - r) - delta
    # SELL targets: YES ask at fv+delta, NO ask at (1-fv)+delta
    yes_ask_target = r + delta
    no_ask_target = (1.0 - r) + delta

    # ── size scaling ────────────────────────────────────────────────────
    # TRENDING: cut size hard (was 0.5) — false TRENDING is common; real trends
    # need even smaller add-size to avoid bagging inventory.
    regime_scale = 0.35 if inp.regime == Regime.TRENDING else 1.0
    tox_scale = 1.0 / (1.0 + inp.toxicity * 12.0)
    common_scale = regime_scale * tox_scale * _clamp(inp.risk_size_scale, 0.0, 1.0)

    soft_cap = p.q_soft_frac  # fraction of q_max at which the adding side pulls
    add_yes = inp.regime not in (Regime.REDUCE_ONLY,) and u < soft_cap
    add_no = inp.regime not in (Regime.REDUCE_ONLY,) and u > -soft_cap
    # For SELL: add YES when we have inventory to offload (u > -soft_cap → long
    # YES → sell YES), or when we want to add short exposure (u < -soft_cap).
    # For SELL, we only enter SELL orders when we have inventory to exit
    # (handled by _maybe_exit below) or when intentionally shorting.
    add_sell_yes = inp.regime not in (Regime.REDUCE_ONLY,) and u > soft_cap
    add_sell_no = inp.regime not in (Regime.REDUCE_ONLY,) and u < -soft_cap

    # Join + hard floor/ceiling: never rest entry orders outside the reward
    # band when the market has a scoring band. Layers that would step out
    # of band are dropped — dust 0.001 orders earn zero rewards.
    band_active = reward_band > 0
    join_dist = reward_band if band_active else None
    # Floor/ceiling: BUY at fv-band (bottom), SELL at fv+band (top).
    yes_band_lo = (inp.fv - reward_band) if band_active else None
    yes_band_hi = (inp.fv + reward_band) if band_active else None
    no_fv = 1.0 - inp.fv
    no_band_lo = (no_fv - reward_band) if band_active else None
    no_band_hi = (no_fv + reward_band) if band_active else None

    # entry: BUY YES
    if add_yes:
        price = _place_bid(
            yes_bid_target, inp.yes_view, tick, dec, inp.fv, p.min_edge_ticks,
            max_join_distance=join_dist,
            band_lo=yes_band_lo,
        )
        if price is not None:
            _add_layers(quotes, m.yes.token_id, Side.BUY, price, tick, dec,
                        _size_shares(p.base_size_usdc, price, common_scale * (1 - max(u, 0.0)), m),
                        p.layers, p.layer_step_ticks, down=True,
                        exchange_min=m.min_order_size, reward_floor=reward_floor,
                        band_lo=yes_band_lo,
                        max_orders=p.max_open_orders_per_market)

    # entry: SELL YES (when we have YES inventory to exit or want to short)
    if add_sell_yes and inp.pos_yes.size >= m.min_order_size:
        price = _place_ask(
            yes_ask_target, inp.yes_view, tick, dec, inp.fv, p.min_edge_ticks,
            max_join_distance=join_dist,
            band_hi=yes_band_hi,
        )
        if price is not None:
            _add_layers(quotes, m.yes.token_id, Side.SELL, price, tick, dec,
                        _size_shares(p.base_size_usdc, price, common_scale * (1 - max(-u, 0.0)), m),
                        p.layers, p.layer_step_ticks, down=False,
                        exchange_min=m.min_order_size, reward_floor=reward_floor,
                        band_lo=yes_band_hi,
                        max_orders=p.max_open_orders_per_market)

    # entry: BUY NO
    if add_no:
        price = _place_bid(
            no_bid_target, inp.no_view, tick, dec, no_fv, p.min_edge_ticks,
            max_join_distance=join_dist,
            band_lo=no_band_lo,
        )
        if price is not None:
            _add_layers(quotes, m.no.token_id, Side.BUY, price, tick, dec,
                        _size_shares(p.base_size_usdc, price, common_scale * (1 - max(-u, 0.0)), m),
                        p.layers, p.layer_step_ticks, down=True,
                        exchange_min=m.min_order_size, reward_floor=reward_floor,
                        band_lo=no_band_lo,
                        max_orders=p.max_open_orders_per_market)

    # entry: SELL NO (when we have NO inventory to exit or want to short)
    if add_sell_no and inp.pos_no.size >= m.min_order_size:
        price = _place_ask(
            no_ask_target, inp.no_view, tick, dec, no_fv, p.min_edge_ticks,
            max_join_distance=join_dist,
            band_hi=no_band_hi,
        )
        if price is not None:
            _add_layers(quotes, m.no.token_id, Side.SELL, price, tick, dec,
                        _size_shares(p.base_size_usdc, price, common_scale * (1 - max(u, 0.0)), m),
                        p.layers, p.layer_step_ticks, down=False,
                        exchange_min=m.min_order_size, reward_floor=reward_floor,
                        band_lo=no_band_hi,
                        max_orders=p.max_open_orders_per_market)

    # ── exits: SELL held inventory (maker, never cross) ─────────────────
    _maybe_exit(quotes, m.yes.token_id, inp.pos_yes, inp.fv, delta, inp.yes_view, tick, dec,
                inp.yes_exit_urgency, m, inp.regime)
    _maybe_exit(quotes, m.no.token_id, inp.pos_no, 1.0 - inp.fv, delta, inp.no_view, tick, dec,
                inp.no_exit_urgency, m, inp.regime)

    return TargetQuotes(cid, inp.regime, tuple(quotes))


# ── helpers ─────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _place_bid(
    target: float, view: BookView, tick: float, dec: int, fv: float, min_edge_ticks: int,
    *, max_join_distance: float | None = None,
    band_lo: float | None = None,
) -> float | None:
    """Position a BUY: join the touch or sit behind, never cross, keep min edge vs FV.

    max_join_distance: if set, only join best_bid when it is within that
    distance of FV (prevents chasing dust bids at 0.001 on thin books).
    band_lo: hard floor for reward-band eligibility; after all adjustments the
    bid is raised to band_lo (or dropped if that would violate min-edge / cross).
    """
    price = target
    # never bid above (FV - min_edge*tick): we don't pay through fair value
    edge_cap = fv - min_edge_ticks * tick
    price = min(price, edge_cap)
    # join the queue rather than jump it (conservative maker default) —
    # but never follow a junk bid far below FV (kills reward-band uptime).
    if view.best_bid is not None and price >= view.best_bid:
        bb = view.best_bid
        if max_join_distance is None or abs(bb - fv) <= max_join_distance:
            price = bb
    # never cross the ask
    if view.best_ask is not None and price >= view.best_ask:
        price = view.best_ask - tick
    # Hard reward-band floor AFTER join/cross so dust best_bids cannot pull us
    # to 0.001 (production OOB regression on livecfg tape).
    if band_lo is not None:
        price = max(price, band_lo)
        price = min(price, edge_cap)
        if view.best_ask is not None and price >= view.best_ask:
            # cannot be in-band without crossing — skip this side
            return None
    p = round_to_tick(price, tick, dec, up=False)
    if p <= 0 or p >= 1:
        return None
    if band_lo is not None and p + 1e-12 < band_lo:
        return None
    return p


def _place_ask(
    target: float, view: BookView, tick: float, dec: int, fv: float, min_edge_ticks: int,
    *, max_join_distance: float | None = None,
    band_hi: float | None = None,
) -> float | None:
    """Position a SELL: join the touch or sit behind, never cross, keep min edge vs FV.

    Mirror of _place_bid for SELL orders:
    - Start at the target price (f(target) = fv + delta for SELL)
    - Never sell below (FV + min_edge*tick): we don't sell through fair value
    - Join the best ask (or sit behind if too far from FV)
    - Never cross the bid
    - Hard reward-band CEILING: if band_hi is set, cap the price at
      band_hi so we stay inside the reward band

    band_hi: hard ceiling for reward-band eligibility; if the joined/sized
    price would exceed band_hi, we cap at band_hi (or drop if that would
    violate min-edge / cross).
    """
    price = target
    # never sell below (FV + min_edge*tick): we don't sell through fair value
    edge_floor = fv + min_edge_ticks * tick
    price = max(price, edge_floor)
    # join the queue rather than jump it (conservative maker default) —
    # but never follow a junk ask far above FV (kills reward-band uptime).
    if view.best_ask is not None and price >= view.best_ask:
        ba = view.best_ask
        if max_join_distance is None or abs(ba - fv) <= max_join_distance:
            price = ba
    # never cross the bid
    if view.best_bid is not None and price <= view.best_bid:
        price = view.best_bid + tick
    # Hard reward-band CEILING AFTER join/cross so dust best_asks cannot
    # pull us above the band (production OOB regression on livecfg tape).
    if band_hi is not None:
        price = min(price, band_hi)
        price = max(price, edge_floor)
        if view.best_bid is not None and price <= view.best_bid:
            # cannot be in-band without crossing — skip this side
            return None
    p = round_to_tick(price, tick, dec, up=True)
    if p <= 0 or p >= 1:
        return None
    if band_hi is not None and p - 1e-12 > band_hi:
        return None
    return p


def _size_shares(base_usdc: float, price: float, scale: float, m: MarketMeta) -> float:
    """USDC-notional sizing -> shares. Per-order minimums applied in _add_layers
    (reward scoring is per ORDER, so the floor must hold per layer, not per total)."""
    shares = (base_usdc / max(price, m.tick_size)) * max(scale, 0.0)
    return round(shares, 2) if shares > 0 else 0.0


def _add_layers(
    quotes: list[Quote], token_id: str, side: Side, top_price: float, tick: float, dec: int,
    total_size: float, layers: int, step_ticks: int, *, down: bool,
    exchange_min: float = 0.0, reward_floor: float = 0.0,
    band_lo: float | None = None,
    max_orders: int = 0,
) -> None:
    """Split size across `layers` price levels stepping away from the touch.

    Each ORDER must meet the exchange min and, when within reach (>=50% of it),
    is bumped to `reward_floor` (the reward min-size × the profile margin) so it
    actually scores — the program scores per order, so a floor applied to the
    total is worthless. Layers that can't reach the floor are consolidated into
    fewer, larger orders rather than resting unscoring dust.

    band_lo: for BUY layers stepping down, stop once price would leave the
    reward band (no dust OOB layers). For SELL layers stepping up, this is
    treated as a CEILING — stop once price would exceed band_lo.

    max_orders: hard cap on number of orders created (0 = unlimited). Prevents
    order book accumulation when the strategy requotes on every book change.
    """
    if total_size <= 0:
        return
    # Early return: if even the full total size is below the exchange minimum,
    # don't create any orders. Wasting API calls on sub-min orders is a net
    # loss (rate budget + rejected fills). The old code checked per-layer
    # *after* dividing, which still created orders that were too small.
    if exchange_min > 0 and total_size < exchange_min:
        return
    # Hard cap on number of orders to prevent accumulation.
    if max_orders > 0:
        layers = min(layers, max_orders)
    layers = max(1, layers)
    reward_floor = max(reward_floor, exchange_min)
    # Count how many layers stay inside band_lo before splitting size.
    # For BUY (down=True): price steps down from top_price; stop if price
    # would fall below band_lo. For SELL (down=False): price steps up from
    # top_price; stop if price would exceed band_lo.
    if band_lo is not None:
        max_steps = 0
        n0 = round(top_price / tick)
        for i in range(layers):
            px = round((n0 - i * step_ticks if down else n0 + i * step_ticks) * tick, dec)
            if down and (px + 1e-12 < band_lo or px <= 0):
                break
            if not down and (px - 1e-12 > band_lo or px >= 1):
                break
            max_steps += 1
        layers = max(1, max_steps) if max_steps > 0 else 0
        if layers <= 0:
            return
    per = round(total_size / layers, 2)
    # consolidate: if a split layer would fall below half the reward floor,
    # use fewer layers so each resting order can still score
    while layers > 1 and reward_floor > 0 and per < 0.5 * reward_floor:
        layers -= 1
        per = round(total_size / layers, 2)
    if reward_floor > 0 and 0.5 * reward_floor <= per < reward_floor:
        per = reward_floor  # bump each order up to scoring size
    if per < exchange_min or per <= 0:
        return
    # Apply max_orders cap after consolidation too.
    if max_orders > 0:
        layers = min(layers, max_orders)
    # Compute prices as integer tick multiples to avoid per-layer round() calls.
    # top_price is already tick-aligned (from round_to_tick); stepping by
    # step_ticks*tick keeps every layer on-grid. round() only cleans FP residue.
    n = round(top_price / tick)
    for i in range(layers):
        ni = n - i * step_ticks if down else n + i * step_ticks
        price = round(ni * tick, dec)
        if band_lo is not None and down and price + 1e-12 < band_lo:
            break
        if band_lo is not None and not down and price - 1e-12 > band_lo:
            break
        if 0 < price < 1:
            quotes.append(Quote(token_id, side, price, per))


def _maybe_exit(
    quotes: list[Quote], token_id: str, pos: Position, token_fv: float, delta: float,
    view: BookView, tick: float, dec: int, urgency: float, m: MarketMeta, regime: Regime,
) -> None:
    if pos.size < m.min_order_size:
        return
    # target starts at fv + delta and walks toward best_bid + tick as urgency -> 1
    passive = token_fv + delta
    floor = (view.best_bid + tick) if view.best_bid is not None else passive
    if regime == Regime.REDUCE_ONLY:
        urgency = max(urgency, 0.5)
    target = passive * (1.0 - urgency) + floor * urgency
    # never cross down through the bid; never sell below best_bid
    if view.best_bid is not None:
        target = max(target, view.best_bid + tick)
    price = round_to_tick(target, tick, dec, up=True)
    # FLOOR (never round up): selling more than we hold is rejected by the
    # exchange -> the exit silently fails and we stay long. Floor guarantees
    # size <= held.
    size = math.floor(pos.size * 100) / 100
    if 0 < price < 1 and size >= m.min_order_size:
        quotes.append(Quote(token_id, Side.SELL, price, size))
