"""Regime-conditional strategy dispatch — four quoting modes, not one with knobs.

Pillar 2 of the S-tier architecture: the regime machine already classifies the
market into five states; this module provides a different quoting function per
trading-relevant regime so the bot doesn't just tweak knobs — it changes behavior
fundamentally.

Strategy dispatch:
  HALTED / EVENT          → empty targets (cancel all) — same as before
  TOXIC / TRENDING         → ToxicStrategy: wide/no quotes, aggressive exit
  REDUCE_ONLY              → ExitOnlyStrategy: aggressive exits, no entries
  QUIET (benign)           → BenignStrategy: tight layered quoting
  QUIET + near-resolve     → ConvexityStrategy: position for 0/1 outcome
  QUIET + queue-war        → QueueWarStrategy: jump-ahead, rebate harvesting

The engine calls dispatch_strategy() instead of construct_quotes() directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Quote, Regime, Side, TargetQuotes
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.quoting import (
    _add_layers,
    _clamp,
    _maybe_exit,
    _place_ask,
    _place_bid,
    _size_shares,
    round_to_tick,
)

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """All the inputs a regime strategy needs to produce TargetQuotes."""

    meta: MarketMeta
    regime: Regime
    fv: float
    vol_short: float
    toxicity: float
    yes_view: BookView
    no_view: BookView
    pos_yes: Position
    pos_no: Position
    profile: StrategyProfile
    now: float
    risk_size_scale: float = 1.0
    yes_exit_urgency: float = 0.0
    no_exit_urgency: float = 0.0
    fill_model_skip: set[str] | None = None  # token_ids that the fill model wants skipped
    hours_to_resolve: float | None = None


def dispatch_strategy(ctx: StrategyContext) -> TargetQuotes:
    """Route to the correct strategy function based on regime + market state."""
    cid = ctx.meta.condition_id

    if ctx.regime in (Regime.EVENT, Regime.HALTED):
        return TargetQuotes(cid, ctx.regime, ())

    if ctx.regime == Regime.REDUCE_ONLY:
        return _exit_only_strategy(ctx)

    near_resolve = (
        ctx.hours_to_resolve is not None
        and ctx.hours_to_resolve <= ctx.profile.reduce_only_hours * 1.5
    )

    in_queue_war = _detect_queue_war(ctx)

    if ctx.regime == Regime.TRENDING or ctx.toxicity > 0.15:
        return _toxic_strategy(ctx)
    if near_resolve:
        return _convexity_strategy(ctx)
    if in_queue_war:
        return _queue_war_strategy(ctx)

    return _benign_strategy(ctx)


# ── Strategy implementations ──────────────────────────────────────────────


def _toxic_strategy(ctx: StrategyContext) -> TargetQuotes:
    """TOXIC/TRENDING: provide minimal liquidity, size cuts, aggressive exits.

    Do NOT provide two-sided quotes. Only rest on the safe side of the book
    (away from toxic flow). Exits are aggressive.
    """
    m = ctx.meta
    p = ctx.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    quotes: list[Quote] = []

    net_shares = ctx.pos_yes.size - ctx.pos_no.size
    q_max_shares = p.q_max_usdc / max(ctx.fv, tick)

    # Toxic: exit urgency always ≥ 0.8 — get out fast
    exit_yes = max(ctx.yes_exit_urgency, 0.8)
    exit_no = max(ctx.no_exit_urgency, 0.8)

    _maybe_exit(quotes, m.yes.token_id, ctx.pos_yes, ctx.fv, p.delta_min_ticks * tick * 3,
                ctx.yes_view, tick, dec, exit_yes, m, Regime.TRENDING)
    _maybe_exit(quotes, m.no.token_id, ctx.pos_no, 1.0 - ctx.fv, p.delta_min_ticks * tick * 3,
                ctx.no_view, tick, dec, exit_no, m, Regime.TRENDING)

    if ctx.regime == Regime.TRENDING and ctx.toxicity <= 0.15:
        u = _clamp(net_shares / q_max_shares, -1.0, 1.0) if q_max_shares > 0 else 0.0
        flow_z = getattr(ctx, "flow_z", 0.0) if hasattr(ctx, "flow_z") else 0.0
        tox_scale = 1.0 / (1.0 + ctx.toxicity * 12.0)
        size_scale = 0.15 * tox_scale * _clamp(ctx.risk_size_scale, 0.0, 1.0)
        delta = max(tick * 5, p.delta_min_ticks * tick * 2)

        if flow_z < 0 and u < 0.5:
            px = _place_bid(ctx.fv - delta, ctx.yes_view, tick, dec, ctx.fv, p.min_edge_ticks)
            if px is not None and m.yes.token_id not in (ctx.fill_model_skip or set()):
                sz = _size_shares(p.base_size_usdc * 0.3, px, size_scale, m)
                _add_layers(quotes, m.yes.token_id, Side.BUY, px, tick, dec, sz,
                            p.layers, p.layer_step_ticks, down=True,
                            exchange_min=m.min_order_size, max_orders=1)
        if flow_z > 0 and u > -0.5:
            no_fv = 1.0 - ctx.fv
            px = _place_bid(no_fv - delta, ctx.no_view, tick, dec, no_fv, p.min_edge_ticks)
            if px is not None and m.no.token_id not in (ctx.fill_model_skip or set()):
                sz = _size_shares(p.base_size_usdc * 0.3, px, size_scale, m)
                _add_layers(quotes, m.no.token_id, Side.BUY, px, tick, dec, sz,
                            p.layers, p.layer_step_ticks, down=True,
                            exchange_min=m.min_order_size, max_orders=1)

    return TargetQuotes(cid, ctx.regime, tuple(quotes))


def _exit_only_strategy(ctx: StrategyContext) -> TargetQuotes:
    """REDUCE_ONLY: aggressive exits only, no new entries."""
    m = ctx.meta
    p = ctx.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    quotes: list[Quote] = []
    exit_yes = max(ctx.yes_exit_urgency, 0.6)
    exit_no = max(ctx.no_exit_urgency, 0.6)

    _maybe_exit(quotes, m.yes.token_id, ctx.pos_yes, ctx.fv, p.delta_min_ticks * tick,
                ctx.yes_view, tick, dec, exit_yes, m, Regime.REDUCE_ONLY)
    _maybe_exit(quotes, m.no.token_id, ctx.pos_no, 1.0 - ctx.fv, p.delta_min_ticks * tick,
                ctx.no_view, tick, dec, exit_no, m, Regime.REDUCE_ONLY)

    return TargetQuotes(cid, ctx.regime, tuple(quotes))


def _convexity_strategy(ctx: StrategyContext) -> TargetQuotes:
    """Near resolve: position for the 0/1 binary outcome, stop providing mid.

    If FV > 0.9: quote BUY YES only (expect YES resolve). Exit NO.
    If FV < 0.1: quote BUY NO only (expect NO resolve). Exit YES.
    Mid-range: quote wider, smaller, exits only.
    """
    m = ctx.meta
    p = ctx.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    quotes: list[Quote] = []

    exit_yes = max(ctx.yes_exit_urgency, 0.7)
    exit_no = max(ctx.no_exit_urgency, 0.7)

    if ctx.fv > 0.9:
        _maybe_exit(quotes, m.no.token_id, ctx.pos_no, 1.0 - ctx.fv, tick,
                    ctx.no_view, tick, dec, exit_no, m, Regime.REDUCE_ONLY)
        delta = max(tick, p.delta_min_ticks * tick)
        px = _place_bid(ctx.fv - delta * 2, ctx.yes_view, tick, dec, ctx.fv, p.min_edge_ticks)
        if px is not None and m.yes.token_id not in (ctx.fill_model_skip or set()):
            sz = _size_shares(p.base_size_usdc * 0.5, px, 0.5, m)
            _add_layers(quotes, m.yes.token_id, Side.BUY, px, tick, dec, sz, 1,
                        p.layer_step_ticks, down=True, exchange_min=m.min_order_size, max_orders=1)
    elif ctx.fv < 0.1:
        _maybe_exit(quotes, m.yes.token_id, ctx.pos_yes, ctx.fv, tick,
                    ctx.yes_view, tick, dec, exit_yes, m, Regime.REDUCE_ONLY)
        no_fv = 1.0 - ctx.fv
        delta = max(tick, p.delta_min_ticks * tick)
        px = _place_bid(no_fv - delta * 2, ctx.no_view, tick, dec, no_fv, p.min_edge_ticks)
        if px is not None and m.no.token_id not in (ctx.fill_model_skip or set()):
            sz = _size_shares(p.base_size_usdc * 0.5, px, 0.5, m)
            _add_layers(quotes, m.no.token_id, Side.BUY, px, tick, dec, sz, 1,
                        p.layer_step_ticks, down=True, exchange_min=m.min_order_size, max_orders=1)
    else:
        _maybe_exit(quotes, m.yes.token_id, ctx.pos_yes, ctx.fv, p.delta_min_ticks * tick * 2,
                    ctx.yes_view, tick, dec, exit_yes, m, Regime.REDUCE_ONLY)
        _maybe_exit(quotes, m.no.token_id, ctx.pos_no, 1.0 - ctx.fv, p.delta_min_ticks * tick * 2,
                    ctx.no_view, tick, dec, exit_no, m, Regime.REDUCE_ONLY)

    return TargetQuotes(cid, ctx.regime, tuple(quotes))


def _queue_war_strategy(ctx: StrategyContext) -> TargetQuotes:
    """Queue-war: jump ahead of competitors at the touch, rebate harvest.

    Book is liquid with multiple competing makers near the touch. Strategy:
    - Match or slightly improve the best bid/ask to get queue priority
    - Use small size to minimize adverse selection exposure
    - Harvest maker rebates from fills, not spread capture
    """
    m = ctx.meta
    p = ctx.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    quotes: list[Quote] = []

    delta = max(tick, p.delta_min_ticks * tick)

    yes_px = None
    if ctx.yes_view.best_bid is not None:
        yes_px = ctx.yes_view.best_bid + tick
        yes_px = min(yes_px, ctx.fv - p.min_edge_ticks * tick)
    if yes_px is None:
        yes_px = ctx.fv - delta
    yes_px = _place_bid(yes_px, ctx.yes_view, tick, dec, ctx.fv, p.min_edge_ticks,
                         join_best_bid=True)
    if yes_px is not None and m.yes.token_id not in (ctx.fill_model_skip or set()):
        sz = _size_shares(p.base_size_usdc * 0.4, yes_px, 0.6, m)
        _add_layers(quotes, m.yes.token_id, Side.BUY, yes_px, tick, dec, sz,
                    min(p.layers, 2), p.layer_step_ticks, down=True,
                    exchange_min=m.min_order_size, max_orders=2)

    no_fv = 1.0 - ctx.fv
    no_px = None
    if ctx.no_view.best_bid is not None:
        no_px = ctx.no_view.best_bid + tick
        no_px = min(no_px, no_fv - p.min_edge_ticks * tick)
    if no_px is None:
        no_px = no_fv - delta
    no_px = _place_bid(no_px, ctx.no_view, tick, dec, no_fv, p.min_edge_ticks,
                        join_best_bid=True)
    if no_px is not None and m.no.token_id not in (ctx.fill_model_skip or set()):
        sz = _size_shares(p.base_size_usdc * 0.4, no_px, 0.6, m)
        _add_layers(quotes, m.no.token_id, Side.BUY, no_px, tick, dec, sz,
                    min(p.layers, 2), p.layer_step_ticks, down=True,
                    exchange_min=m.min_order_size, max_orders=2)

    if ctx.pos_yes.size > 0:
        _maybe_exit(quotes, m.yes.token_id, ctx.pos_yes, ctx.fv, delta,
                    ctx.yes_view, tick, dec, ctx.yes_exit_urgency, m, ctx.regime)
    if ctx.pos_no.size > 0:
        _maybe_exit(quotes, m.no.token_id, ctx.pos_no, 1.0 - ctx.fv, delta,
                    ctx.no_view, tick, dec, ctx.no_exit_urgency, m, ctx.regime)

    return TargetQuotes(cid, ctx.regime, tuple(quotes))


def _benign_strategy(ctx: StrategyContext) -> TargetQuotes:
    """QUIET, benign market: tight layered quoting, full size, accumulate.

    This is the default farming posture. Delegates to the existing
    construct_quotes logic (which is already optimized for QUIET regime).
    """
    from polymaker.strategy.quoting import QuoteInputs, construct_quotes

    m = ctx.meta

    net_shares = ctx.pos_yes.size - ctx.pos_no.size
    q_max_shares = ctx.profile.q_max_usdc / max(ctx.fv, m.tick_size)
    u = _clamp(net_shares / q_max_shares, -1.0, 1.0) if q_max_shares > 0 else 0.0

    add_yes = u < ctx.profile.q_soft_frac
    add_no = u > -ctx.profile.q_soft_frac

    skip: set[str] = ctx.fill_model_skip or set()
    if not add_yes:
        skip.add(m.yes.token_id)
    if not add_no:
        skip.add(m.no.token_id)

    inp = QuoteInputs(
        meta=m,
        regime=Regime.QUIET,
        fv=ctx.fv,
        vol_short=ctx.vol_short,
        toxicity=ctx.toxicity,
        yes_view=ctx.yes_view,
        no_view=ctx.no_view,
        pos_yes=ctx.pos_yes,
        pos_no=ctx.pos_no,
        profile=ctx.profile,
        now=ctx.now,
        risk_size_scale=ctx.risk_size_scale,
        yes_exit_urgency=ctx.yes_exit_urgency,
        no_exit_urgency=ctx.no_exit_urgency,
        intel_skip=bool(skip),
    )

    targets = construct_quotes(inp)

    if skip:
        filtered = tuple(q for q in targets.quotes if q.token_id not in skip)
        return TargetQuotes(targets.condition_id, targets.regime, filtered)

    return targets


# ── helpers ──────────────────────────────────────────────────────────────


def _detect_queue_war(ctx: StrategyContext) -> bool:
    """Detect if we're in a queue-war: multiple makers competing at/near touch."""
    yv = ctx.yes_view
    nv = ctx.no_view

    if yv.best_bid is None or yv.best_ask is None:
        return False
    if nv.best_bid is None or nv.best_ask is None:
        return False

    spread_yes = yv.best_ask - yv.best_bid
    spread_no = nv.best_ask - nv.best_bid
    tick = ctx.meta.tick_size

    tight = spread_yes <= 3 * tick and spread_no <= 3 * tick
    deep = yv.bid_depth > ctx.profile.base_size_usdc * 5
    liquid = ctx.vol_short < 0.01 and ctx.toxicity < 0.05

    return tight and deep and liquid
