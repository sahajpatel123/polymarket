"""Unified advanced quoting: Avellaneda-Stoikov pricing + Kelly sizing.

Combines the optimal market-making model (Avellaneda-Stoikov) with the
optimal position sizing model (Kelly-inspired) to produce fast, accurate
quotes that account for inventory, volatility, edge, and bankroll.

This is an alternative to the simpler linear skew + fixed-size approach
in `strategy/quoting.py`. It's more accurate because it:
1. Uses the theoretically optimal spread (Avellaneda-Stoikov)
2. Adapts to inventory, volatility, and time horizon
3. Sizes positions based on edge and risk (Kelly)
4. Caps by maximum inventory and bankroll

Pure functions only — no I/O. The engine can opt to use this instead of
the simpler `construct_quotes` by calling `compute_advanced_quotes`.

Reference:
- Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book"
- Kelly (1956), "A new interpretation of information rate"
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.avellaneda_stoikov import (
    ASInputs,
    avellaneda_stoikov,
    gamma_from_profile,
    kappa_from_liquidity,
)
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
    fv: float                    # fair value (YES)
    sigma: float                 # per-second volatility
    yes_view: BookView
    no_view: BookView
    pos_yes: Position
    pos_no: Position
    profile: StrategyProfile
    bankroll_usdc: float         # available capital
    now: float


@dataclass(frozen=True, slots=True)
class AdvancedQuoteOutput:
    """Output of the advanced quoting model."""
    bid: float                   # optimal YES bid price
    ask: float                   # optimal YES ask price
    size_yes_shares: float       # recommended YES size
    size_no_shares: float        # recommended NO size
    reservation: float           # Avellaneda-Stoikov reservation price
    half_spread: float           # optimal half-spread
    edge_ratio: float            # signal-to-noise ratio


def compute_advanced_quotes(inp: AdvancedQuoteInputs) -> AdvancedQuoteOutput:
    """Compute optimal bid/ask and sizes using Avellaneda-Stoikov + Kelly.

    The model:
    1. Uses Avellaneda-Stoikov to compute the optimal reservation price
       and half-spread, accounting for inventory, volatility, and time
    2. Uses Kelly-inspired sizing to determine the number of shares,
       accounting for edge, volatility, inventory, and bankroll
    3. Returns the optimal bid/ask prices and recommended sizes

    The bid is for buying YES (we go long YES), and the NO bid is
    derived from the YES reservation (we go long NO by buying NO at
    1 - ask_yes).
    """
    m = inp.meta
    p = inp.profile
    tick = m.tick_size
    dec = m.price_decimals

    # Net inventory in YES-equivalent shares
    net_shares = inp.pos_yes.size - inp.pos_no.size
    max_inv = p.q_max_usdc / max(inp.fv, tick)

    # Time horizon: estimate from market liquidity
    T = time_horizon_from_liquidity(m.liquidity_num)

    # Risk aversion and arrival rate
    gamma = gamma_from_profile(p.gamma)
    kappa = kappa_from_liquidity(m.liquidity_num)

    # Avellaneda-Stoikov for YES side
    as_yes = avellaneda_stoikov(ASInputs(
        mid=inp.fv,
        inventory=net_shares,
        sigma=inp.sigma,
        time_horizon_s=T,
        gamma=gamma,
        kappa=kappa,
    ))

    # Round to tick
    bid_yes = _round_to_tick(as_yes.bid, tick, dec, up=False)
    ask_yes = _round_to_tick(as_yes.ask, tick, dec, up=True)

    # Clamp to valid range
    bid_yes = max(tick, min(bid_yes, 1.0 - tick))
    ask_yes = max(tick, min(ask_yes, 1.0 - tick))

    # Edge per share: half the spread
    edge = edge_from_spread(as_yes.half_spread, tick)

    # Kelly sizing for YES
    kelly_yes = kelly_size(KellyInputs(
        edge=edge,
        sigma=inp.sigma,
        time_horizon_s=T,
        bankroll_usdc=inp.bankroll_usdc * 0.5,  # split between YES and NO
        inventory_shares=inp.pos_yes.size,
        max_inventory_shares=max_inv,
        kelly_fraction=0.25,  # quarter-Kelly for safety
        price=inp.fv,
    ))

    # Kelly sizing for NO (NO price = 1 - YES price)
    no_price = 1.0 - inp.fv
    kelly_no = kelly_size(KellyInputs(
        edge=edge,
        sigma=inp.sigma,
        time_horizon_s=T,
        bankroll_usdc=inp.bankroll_usdc * 0.5,
        inventory_shares=inp.pos_no.size,
        max_inventory_shares=max_inv,
        kelly_fraction=0.25,
        price=no_price,
    ))

    return AdvancedQuoteOutput(
        bid=bid_yes,
        ask=ask_yes,
        size_yes_shares=kelly_yes.size_shares,
        size_no_shares=kelly_no.size_shares,
        reservation=as_yes.reservation,
        half_spread=as_yes.half_spread,
        edge_ratio=kelly_yes.edge_ratio,
    )


def _round_to_tick(price: float, tick: float, decimals: int, *, up: bool) -> float:
    """Round a price to the tick grid."""
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, decimals)
