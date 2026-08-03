"""Pure quote construction: (market state, inventory, params) -> TargetQuotes.

This is the deterministic core of the strategy. No I/O, no wall-clock reads
except values passed in. Everything here is exercised directly by unit tests.

Model (see the README):
  reservation  r  = FV - skew(inventory)
  half-spread  δ  = base + c_vol·σ + c_tox·toxicity + c_kyle·AS(λ)
                    (clamped to reward band in QUIET)
  YES entry bid   = r - δ                       (BUY YES, USDC-collateralized)
  NO  entry bid   = (1 - r) - δ                  (BUY NO; implied YES ask at r + δ)
  exits           = SELL limits on held inventory, walked toward the touch by urgency

The BUY-YES + BUY-NO pair is the canonical two-sided quote: both are bids, both
score rewards, and a filled pair merges back to USDC at locked edge 1 - p - q.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
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
    # Kyle λ (price impact / share); used only when profile.c_kyle > 0.
    kyle_lambda: float = 0.0
    # Intelligence layer (DecisionFramework) — optional judgment knobs
    intel_size_scale: float = 1.0  # extra size mult from brain (0 → empty entries)
    # None = intelligence not controlling band (legacy economic target).
    # When set: 0.0 = rest at band floor (most passive); 1.0 = near FV − min_edge.
    intel_buy_band_frac: float | None = None
    # Optional extra half-spread mult from brain (1.0 = no change).
    intel_spread_mult: float = 1.0
    # Optional BUY offset in ticks vs FV (negative = below FV). Applied after
    # band frac so adaptive passive offsets can widen further from mid.
    intel_buy_offset_ticks: int | None = None
    intel_skip: bool = False  # brain says do not quote entries
    # Kelly sizing (single path, no double-counting).
    # When both are > 0, Kelly scales the position size proportional to
    # edge/variance (optimal growth rate). Set to 0 to use profile base_size only.
    kelly_fraction: float = 0.0
    bankroll_usdc: float = 0.0
    liquidity: float = 0.0  # for time-horizon estimation


def _kelly_multiplier(
    edge: float, sigma: float, time_horizon_s: float,
    bankroll_usdc: float, inventory_shares: float, max_inventory_shares: float,
    kelly_fraction: float, price: float,
) -> float:
    """Compute a size multiplier from Kelly-optimal sizing.

    Returns 1.0 when there's not enough data for Kelly. The multiplier
    is applied on top of the profile's base_size_usdc/layers, so this
    is a pure multiplicative adjustment (not a replacement).
    """
    from polymaker.strategy.kelly import KellyInputs, kelly_size
    if bankroll_usdc <= 0 or kelly_fraction <= 0 or sigma <= 0 or time_horizon_s <= 0:
        return 1.0
    inp = KellyInputs(
        edge=max(edge, 1e-6),
        sigma=sigma,
        time_horizon_s=time_horizon_s,
        bankroll_usdc=bankroll_usdc,
        inventory_shares=inventory_shares,
        max_inventory_shares=max(max_inventory_shares, 1.0),
        kelly_fraction=min(kelly_fraction, 1.0),
        price=max(price, 1e-6),
        min_size_shares=0.0,
    )
    out = kelly_size(inp)
    if out.fraction <= 0:
        return 1.0
    # Map Kelly fraction (pct of bankroll to risk) to a size multiplier
    # on the profile's base_size: 2% of bankroll → 1.1x, 20% → 2.0x.
    return min(3.0, max(0.3, 1.0 + out.fraction * 5.0))


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
    # Hard ceiling on any single order's notional. The reward floor is an
    # absolute share count and will otherwise blow straight through the position
    # cap on higher-priced tokens.
    max_market_notional = float(p.q_max_usdc)

    # Inventory skew: quadratic taper near |u|→1 so edge compounds without
    # over-skewing mid-range inventory (linear gamma·σ·u under-reacts at tails).
    # When use_as_reservation_price is True, use Avellaneda-Stoikov optimal
    # reservation price instead: skew = gamma * sigma² * T * inventory (linear).
    if bool(getattr(p, "use_as_reservation_price", False)):
        _t_horizon = max(60.0, min(3600.0, 3600.0 / max(inp.liquidity / 1000.0, 1.0) ** 0.5))
        skew = p.gamma * inp.vol_short ** 2 * _t_horizon * net_shares
    else:
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
    if float(getattr(p, "c_kyle", 0.0) or 0.0) > 0.0 and inp.kyle_lambda > 0.0:
        # Glosten–Milgrom-style AS half-spread proxy: c_kyle * λ * size_proxy
        # (full round-trip AS ≈ 2λq; we add half of that scaled by c_kyle).
        size_proxy = max(reward_floor, p.base_size_usdc / max(inp.fv, tick))
        delta += float(p.c_kyle) * inp.kyle_lambda * size_proxy
    # Intelligence may widen half-spread (toxic/volatile); never shrink below econ.
    spread_mult = max(1.0, float(inp.intel_spread_mult or 1.0))
    delta = delta * spread_mult
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
    # Kelly sizing: single computation, no double-counting.
    # Uses half-spread as edge proxy and accounts for current inventory.
    _kelly_scale = _kelly_multiplier(
        edge=delta,
        sigma=max(inp.vol_short, 1e-6),
        time_horizon_s=max(60.0, min(3600.0, 3600.0 / max(inp.liquidity / 1000.0, 1.0) ** 0.5)),
        bankroll_usdc=max(inp.bankroll_usdc, 1.0),
        inventory_shares=net_shares,
        max_inventory_shares=q_max_shares,
        kelly_fraction=inp.kelly_fraction,
        price=max(inp.fv, tick),
    )
    common_scale = (
        regime_scale
        * tox_scale
        * _kelly_scale
        * _clamp(inp.risk_size_scale, 0.0, 1.0)
        * _clamp(inp.intel_size_scale, 0.0, 2.0)
    )

    soft_cap = p.q_soft_frac  # fraction of q_max at which the adding side pulls
    # intel_skip: brain refuses *new* risk (entries) but never blocks exits —
    # REDUCE_ONLY / inventory unwind must still work on dead/stale tape.
    can_enter = not inp.intel_skip
    add_yes = can_enter and inp.regime not in (Regime.REDUCE_ONLY,) and u < soft_cap
    add_no = can_enter and inp.regime not in (Regime.REDUCE_ONLY,) and u > -soft_cap
    # For SELL: add YES when we have inventory to offload (u > -soft_cap → long
    # YES → sell YES), or when we want to add short exposure (u < -soft_cap).
    # For SELL, we only enter SELL orders when we have inventory to exit
    # (handled by _maybe_exit below) or when intentionally shorting.
    add_sell_yes = can_enter and inp.regime not in (Regime.REDUCE_ONLY,) and u > soft_cap
    add_sell_no = can_enter and inp.regime not in (Regime.REDUCE_ONLY,) and u < -soft_cap

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

    # Intelligence: place BUY between band floor and FV−min_edge.
    # None = leave economic target; 0.0 = floor (most passive); 1.0 = aggressive.
    # Always apply when set so toxic learning (frac=0) is not a no-op.
    if band_active and inp.intel_buy_band_frac is not None:
        frac = _clamp(float(inp.intel_buy_band_frac), 0.0, 1.0)
        edge_cap = inp.fv - p.min_edge_ticks * tick
        lo = yes_band_lo if yes_band_lo is not None else yes_bid_target
        hi = min(edge_cap, yes_band_hi if yes_band_hi is not None else edge_cap)
        if hi > lo:
            yes_bid_target = lo + frac * (hi - lo)
        no_edge = no_fv - p.min_edge_ticks * tick
        nlo = no_band_lo if no_band_lo is not None else no_bid_target
        nhi = min(no_edge, no_band_hi if no_band_hi is not None else no_edge)
        if nhi > nlo:
            no_bid_target = nlo + frac * (nhi - nlo)

    # Adaptive offset: further passive step below FV (BUY only). More negative
    # offset → lower bid; still clamped by band_lo in _place_bid.
    if inp.intel_buy_offset_ticks is not None and tick > 0:
        off = int(inp.intel_buy_offset_ticks)
        # Convention: negative = below FV. If caller passes positive magnitude,
        # treat as "ticks below FV" for BUY safety.
        buy_off = off if off <= 0 else -abs(off)
        yes_bid_target = min(yes_bid_target, inp.fv + buy_off * tick)
        no_bid_target = min(no_bid_target, no_fv + buy_off * tick)

    # entry: BUY YES
    if add_yes:
        price = _place_bid(
            yes_bid_target, inp.yes_view, tick, dec, inp.fv, p.min_edge_ticks,
            max_join_distance=join_dist,
            band_lo=yes_band_lo,
            join_best_bid=bool(p.join_best_bid),
        )
        if price is not None:
            _add_layers(quotes, m.yes.token_id, Side.BUY, price, tick, dec,
                        _size_shares(p.base_size_usdc, price, common_scale * (1 - max(u, 0.0)), m),
                        p.layers, p.layer_step_ticks, down=True,
                        exchange_min=m.min_order_size, reward_floor=reward_floor,
                        band_lo=yes_band_lo,
                        max_orders=p.max_open_orders_per_market,
                        max_notional_usdc=max_market_notional)

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
                        max_orders=p.max_open_orders_per_market,
                        max_notional_usdc=max_market_notional)

    # entry: BUY NO
    if add_no:
        price = _place_bid(
            no_bid_target, inp.no_view, tick, dec, no_fv, p.min_edge_ticks,
            max_join_distance=join_dist,
            band_lo=no_band_lo,
            join_best_bid=bool(p.join_best_bid),
        )
        if price is not None:
            _add_layers(quotes, m.no.token_id, Side.BUY, price, tick, dec,
                        _size_shares(p.base_size_usdc, price, common_scale * (1 - max(-u, 0.0)), m),
                        p.layers, p.layer_step_ticks, down=True,
                        exchange_min=m.min_order_size, reward_floor=reward_floor,
                        band_lo=no_band_lo,
                        max_orders=p.max_open_orders_per_market,
                        max_notional_usdc=max_market_notional)

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
                        max_orders=p.max_open_orders_per_market,
                        max_notional_usdc=max_market_notional)

    # ── exits: SELL held inventory (maker, never cross) ─────────────────
    _maybe_exit(quotes, m.yes.token_id, inp.pos_yes, inp.fv, delta, inp.yes_view, tick, dec,
                inp.yes_exit_urgency, m, inp.regime,
                stop_loss_pct=float(getattr(p, "stop_loss_pct", 0.0) or 0.0))
    _maybe_exit(quotes, m.no.token_id, inp.pos_no, 1.0 - inp.fv, delta, inp.no_view, tick, dec,
                inp.no_exit_urgency, m, inp.regime,
                stop_loss_pct=float(getattr(p, "stop_loss_pct", 0.0) or 0.0))

    return TargetQuotes(cid, inp.regime, tuple(quotes))


# ── helpers ─────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _place_bid(
    target: float, view: BookView, tick: float, dec: int, fv: float, min_edge_ticks: int,
    *, max_join_distance: float | None = None,
    band_lo: float | None = None,
    join_best_bid: bool = False,
) -> float | None:
    """Position a BUY: join the touch or sit behind, never cross, keep min edge vs FV.

    max_join_distance: if set, only join best_bid when it is within that
    distance of FV (prevents chasing dust bids at 0.001 on thin books).
    band_lo: hard floor for reward-band eligibility; after all adjustments the
    bid is raised to band_lo (or dropped if that would violate min-edge / cross).
    join_best_bid: when True, improve up to best_bid even if target was below
    the touch (still capped by edge_cap / ask / band rules).
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
    elif join_best_bid and view.best_bid is not None:
        # Improve toward touch from below when the touch is still safe vs FV.
        bb = view.best_bid
        if bb <= edge_cap + 1e-12 and (
            max_join_distance is None or abs(bb - fv) <= max_join_distance
        ):
            price = max(price, bb)
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
        # The floor-rounded price fell below the reward-band floor (FV sits
        # just past a tick boundary). Round UP to the band floor instead of
        # dropping the side entirely — band_lo is itself tick-aligned, so a
        # valid in-band price always exists at or above it.
        p = round_to_tick(band_lo, tick, dec, up=True)
        if p < band_lo - 1e-12:
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
    max_notional_usdc: float = 0.0,
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
    # The reward bump must not breach the per-market notional cap. Polymarket's
    # reward minimum is an absolute share count (commonly 200), so on a $0.88
    # token one "scoring" order is $176 — one observed run placed 255.68 shares
    # ($225) against a $75 cap. That over-size immediately pushes the market
    # into REDUCE_ONLY, which disables the exit's profit floor, so the position
    # then unwinds at cost: round trips close flat or negative by construction.
    # Rewards are worth forfeiting; a risk cap is not.
    if max_notional_usdc > 0 and top_price > 0:
        cap_shares = max_notional_usdc / top_price
        if per > cap_shares:
            per = round(cap_shares, 2)
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
    *, stop_loss_pct: float = 0.0, min_profit_ticks: int = 1,
) -> None:
    """Quote a maker exit for held inventory.

    Two properties this must have, both of which were missing:

    **Reachable.** The passive target was ``token_fv + delta`` with no cap, so
    with a realistic half-spread the ask rested 9-49 ticks above the book and
    could never be hit. Inventory was therefore never sold — every fill in the
    observed sessions was a BUY. The passive leg is now capped at the touch.

    **P&L-aware.** The old target never referenced ``pos.avg_price``, so the
    exit had no notion of being in profit or in loss: it would walk down to
    ``best_bid + tick`` on a timer regardless of cost. Now the exit will not be
    offered below cost while there is still time, and a breach of the stop takes
    priority over patience.
    """
    if pos.size < m.min_order_size:
        return
    cost = float(pos.avg_price or 0.0)

    # ── stop: fair value has fallen through the stop, leave now ──
    # A post-only maker cannot cross, so the most aggressive legal exit is
    # best_bid + 1 tick. (A resting sell *below* the market never fills, which
    # is why a limit-only "stop" is not a stop at all.)
    #
    # The distance is floored at 2 ticks. A percentage stop is meaningless when
    # it is finer than the grid: 1.5% of a $0.19 asset is 0.003, well under one
    # $0.01 tick, so every single downtick tripped the stop and the position was
    # dumped for a 1-tick (-5.3%) loss on noise.
    stopped = False
    if cost > 0.0 and stop_loss_pct > 0.0:
        stop_dist = max(cost * stop_loss_pct, 2.0 * tick)
        stopped = token_fv <= cost - stop_dist
    if stopped:
        urgency = 1.0

    passive = token_fv + delta
    # Reachability: never park the exit above the current ask.
    if view.best_ask is not None:
        passive = min(passive, view.best_ask)
    floor = (view.best_bid + tick) if view.best_bid is not None else passive
    if regime == Regime.REDUCE_ONLY:
        urgency = max(urgency, 0.5)
    target = passive * (1.0 - urgency) + floor * urgency

    # ── profit protection ──
    # While not stopped and not yet out of time, do not offer below cost: that
    # realises a loss for no reason. Loss-taking is driven by the stop above and
    # by urgency reaching 1.0 (the time stop).
    if (
        cost > 0.0
        and not stopped
        and urgency < 1.0
        and regime != Regime.REDUCE_ONLY
    ):
        target = max(target, cost + min_profit_ticks * tick)

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


# ── Risk-managed TP/SL exit targets ─────────────────────────────────────


def clamp_sell_exposure(
    quotes: Sequence[Quote], held: Mapping[str, float], *, min_order_size: float
) -> list[Quote]:
    """Cap total SELL size per token at the inventory actually held.

    Exit quotes come from three independent places — the inventory unwind in
    ``_maybe_exit`` (which offers the whole position), the take-profit, and the
    stop-loss — and each was capped only against the position *individually*.
    Together they could offer up to 3x the holding. The exchange has no OCO, so
    in live trading the surplus is rejected and in paper every leg can fill,
    turning a long into a short.

    Allocation is by ascending price: the most aggressive (most likely to fill)
    exit is honoured first, so risk reduction wins over the optimistic
    take-profit when inventory is scarce. BUY quotes pass through untouched.
    """
    sells: list[tuple[int, Quote]] = []
    out: list[Quote | None] = list(quotes)
    for i, q in enumerate(quotes):
        if q.side is Side.SELL:
            sells.append((i, q))
    if not sells:
        return list(quotes)

    remaining = {tok: max(0.0, float(sz)) for tok, sz in held.items()}
    # cheapest first == closest to the bid == most likely to be hit
    for i, q in sorted(sells, key=lambda pair: pair[1].price):
        avail = remaining.get(q.token_id)
        if avail is None:
            # selling a token we do not hold is not an exit; leave it alone
            continue
        if avail < min_order_size:
            out[i] = None
            continue
        take = min(q.size, avail)
        remaining[q.token_id] = avail - take
        out[i] = q if take == q.size else dataclasses.replace(q, size=take)
    return [q for q in out if q is not None]


def compute_tp_sl(
    *,
    fill_price: float,
    fill_size: float,
    fv: float,
    tp_pct: float,
    sl_pct: float,
    max_risk_usdc: float,
    tick: float,
    dec: int,
) -> tuple[Quote | None, Quote | None]:
    """Compute take-profit and stop-loss exit quotes for a fill.

    TP: sell at fill_price * (1 + tp_pct), capped at fill_price + 2*tick above FV.
    SL: sell at fill_price * (1 - sl_pct), floored at fill_price - 2*tick below FV.

    If max_risk_usdc > 0 and the SL size would risk more than that, size is
    reduced so max loss ≤ max_risk_usdc. Returns (tp_quote, sl_quote) where
    either may be None if the price would cross/become invalid.
    """
    tp = None
    sl = None

    tp_raw = fill_price * (1.0 + tp_pct) if tp_pct > 0 else 0.0
    if tp_raw > 0:
        # TP uncapped — if someone crosses at our profit target, we take it.
        tp_price = round_to_tick(tp_raw, tick, dec, up=True)
        if 0 < tp_price < 1.0 and tp_price > fill_price:
            tp_size = math.floor(fill_size * 100) / 100
            if tp_size > 0:
                tp = Quote("", Side.SELL, tp_price, tp_size)

    sl_raw = fill_price * (1.0 - sl_pct) if sl_pct > 0 else 0.0
    if sl_raw > 0:
        sl_raw = max(sl_raw, fv - 5.0 * tick)
        sl_price = round_to_tick(sl_raw, tick, dec, up=False)
        if 0 < sl_price < 1.0 and sl_price < fill_price:
            sl_size = math.floor(fill_size * 100) / 100
            if max_risk_usdc > 0 and sl_size > 0:
                risk_per_share = fill_price - sl_price
                if risk_per_share > 0:
                    max_shares = max_risk_usdc / risk_per_share
                    sl_size = min(sl_size, math.floor(max_shares * 100) / 100)
            if sl_size > 0:
                sl = Quote("", Side.SELL, sl_price, sl_size)

    return tp, sl
