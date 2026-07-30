"""Unit tests for pure quote construction — the strategy's decision core."""

from __future__ import annotations

import pytest

from polymaker.domain import Position, Regime, Side
from polymaker.strategy.quoting import (
    QuoteInputs,
    _place_ask,
    _place_bid,
    compute_fair_value,
    construct_quotes,
    round_to_tick,
)
from tests.conftest import view


def _inputs(meta, profile, **over):
    base = dict(
        meta=meta,
        regime=Regime.QUIET,
        fv=0.50,
        vol_short=0.0,
        toxicity=0.0,
        yes_view=view(0.49, 0.51),
        no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"),
        pos_no=Position("no-token"),
        profile=profile,
        now=1000.0,
    )
    base.update(over)
    return QuoteInputs(**base)


# ── round_to_tick ──────────────────────────────────────────────────────────


def test_round_to_tick_down_and_up():
    assert round_to_tick(0.5049, 0.01, 2, up=False) == 0.50
    assert round_to_tick(0.5051, 0.01, 2, up=True) == 0.51
    # clamps inside (0,1)
    assert round_to_tick(0.0, 0.01, 2, up=False) == 0.01
    assert round_to_tick(1.0, 0.01, 2, up=True) == 0.99


def test_place_ask_basic():
    """_place_ask: SELL at target above FV, never below edge floor, never crosses bid."""
    # target $0.52, FV $0.50, no book → just at target
    v = view(None, None)
    p = _place_ask(0.52, v, 0.01, 2, 0.50, 1)  # min_edge_ticks=1
    assert p == 0.52
    # below edge floor (FV + 1 tick = 0.51) → bumped up
    p2 = _place_ask(0.505, v, 0.01, 2, 0.50, 1)
    assert p2 == 0.51


def test_place_ask_joins_best_ask():
    """Joins the best_ask when within range."""
    v = view(0.49, 0.53)  # best_bid=0.49, best_ask=0.53
    p = _place_ask(0.55, v, 0.01, 2, 0.50, 1)
    assert p == 0.53  # joined the ask


def test_place_ask_never_crosses_bid():
    """Won't cross the bid (pulls to best_bid + tick)."""
    v = view(0.55, 0.58)  # best_bid=0.55, best_ask=0.58
    # target 0.56, FV 0.50, min_edge=1 → floor 0.51
    # price=max(0.56, 0.51)=0.56; 0.56>=0.58? No (no join).
    # 0.56<=0.55? No (no cross). result: 0.56.
    p = _place_ask(0.56, v, 0.01, 2, 0.50, 1)
    assert p == 0.56  # no cross, no join
    # Now try a price that WOULD cross: target 0.54, bid 0.55
    # floor 0.51, price=0.54; 0.54<=0.55 → cross → pull to 0.56 (bid+tick)
    v2 = view(0.55, 0.58)
    p2 = _place_ask(0.54, v2, 0.01, 2, 0.50, 1)
    assert p2 == 0.56  # pulled up to best_bid + tick


def test_place_ask_band_hi_ceiling():
    """band_hi caps the price (SELL ceiling)."""
    v = view(None, None)
    # target 0.55, band_hi=0.52 → capped at 0.52
    p = _place_ask(0.55, v, 0.01, 2, 0.50, 1, band_hi=0.52)
    assert p == 0.52


def test_compute_fair_value_flow_nudge():
    # positive flow nudges FV up, negative down, no flow = microprice
    assert compute_fair_value(0.50, 0.0, 0.01) == pytest.approx(0.50)
    assert compute_fair_value(0.50, 1.0, 0.01, weight=0.5) == pytest.approx(0.505)
    assert compute_fair_value(0.50, -1.0, 0.01, weight=0.5) == pytest.approx(0.495)
    assert compute_fair_value(0.50, 1.0, 0.01, weight=0.0) == pytest.approx(0.50)


def test_strategy_profile_flow_fv_weight_default():
    from polymaker.config import StrategyProfile

    assert StrategyProfile().flow_fv_weight == pytest.approx(0.5)
    assert StrategyProfile(flow_fv_weight=0.0).flow_fv_weight == pytest.approx(0.0)


# ── two-sided quoting ────────────────────────────────────────────────────────


def test_quiet_market_quotes_both_sides_as_bids(meta, profile):
    tq = construct_quotes(_inputs(meta, profile))
    assert tq.regime == Regime.QUIET
    yes = [q for q in tq.quotes if q.token_id == "yes-token"]
    no = [q for q in tq.quotes if q.token_id == "no-token"]
    assert yes and no
    # both entry quotes are BUYs (USDC-collateralized two-sided quote)
    assert all(q.side == Side.BUY for q in yes)
    assert all(q.side == Side.BUY for q in no)


def test_pair_prices_sum_below_one(meta, profile):
    """BUY YES @ p and BUY NO @ q must satisfy p + q < 1 (merge edge)."""
    tq = construct_quotes(_inputs(meta, profile))
    top_yes = max(q.price for q in tq.quotes if q.token_id == "yes-token")
    top_no = max(q.price for q in tq.quotes if q.token_id == "no-token")
    assert top_yes + top_no < 1.0


def test_never_bids_through_fair_value(meta, profile):
    """No BUY should ever sit at or above FV - min_edge (YES) / (1-FV)-min_edge (NO)."""
    tq = construct_quotes(_inputs(meta, profile, fv=0.50))
    edge = profile.min_edge_ticks * meta.tick_size
    for q in tq.quotes:
        if q.side == Side.BUY and q.token_id == "yes-token":
            assert q.price <= 0.50 - edge + 1e-9
        if q.side == Side.BUY and q.token_id == "no-token":
            assert q.price <= 0.50 - edge + 1e-9  # NO fv is also 0.50 here


def test_layers_split_size(meta, profile):
    # Wide reward band so layer steps stay in-band (band floor would else collapse layers)
    wide = meta
    from dataclasses import replace
    try:
        wide = replace(meta, rewards_max_spread=10.0)  # 10c band
    except TypeError:
        pass
    tq = construct_quotes(_inputs(wide, profile))
    yes = sorted((q for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY),
                 key=lambda q: -q.price)
    assert len(yes) == profile.layers
    # deeper layer is at a lower price
    assert yes[0].price > yes[1].price


# ── inventory skew ──────────────────────────────────────────────────────────


def test_long_yes_inventory_skews_quotes_down(meta, profile):
    """Holding YES should lower the YES bid and raise the NO bid vs flat."""
    flat = construct_quotes(_inputs(meta, profile, vol_short=0.02))
    longy = construct_quotes(
        _inputs(meta, profile, vol_short=0.02, pos_yes=Position("yes-token", 300, 0.5))
    )

    def top(tq, tok):
        ps = [q.price for q in tq.quotes if q.token_id == tok and q.side == Side.BUY]
        return max(ps) if ps else None

    # YES bid should not be higher when long YES; NO bid should not be lower
    assert top(longy, "yes-token") <= top(flat, "yes-token")
    assert top(longy, "no-token") >= top(flat, "no-token")


def test_reduce_only_emits_only_exits(meta, profile):
    tq = construct_quotes(
        _inputs(
            meta, profile, regime=Regime.REDUCE_ONLY,
            pos_yes=Position("yes-token", 100, 0.5),
        )
    )
    assert all(q.side == Side.SELL for q in tq.quotes)
    assert any(q.token_id == "yes-token" for q in tq.quotes)


def test_event_and_halted_pull_all_quotes(meta, profile):
    for regime in (Regime.EVENT, Regime.HALTED):
        tq = construct_quotes(
            _inputs(meta, profile, regime=regime, pos_yes=Position("yes-token", 100, 0.5))
        )
        assert tq.is_empty


# ── exits ────────────────────────────────────────────────────────────────────


def test_exit_sell_priced_above_fv_when_not_urgent(meta, profile):
    tq = construct_quotes(
        _inputs(meta, profile, pos_yes=Position("yes-token", 100, 0.4), yes_exit_urgency=0.0)
    )
    sells = [q for q in tq.quotes if q.side == Side.SELL and q.token_id == "yes-token"]
    assert sells
    assert sells[0].price >= 0.50  # at/above FV, a passive maker exit


def test_exit_never_below_best_bid(meta, profile):
    tq = construct_quotes(
        _inputs(
            meta, profile,
            pos_yes=Position("yes-token", 100, 0.4),
            yes_view=view(0.49, 0.51),
            yes_exit_urgency=1.0,  # maximally urgent
        )
    )
    sells = [q for q in tq.quotes if q.side == Side.SELL and q.token_id == "yes-token"]
    assert sells
    assert sells[0].price >= 0.49  # still a maker order, never crosses down


def test_no_exit_when_position_is_dust(meta, profile):
    tq = construct_quotes(
        _inputs(meta, profile, pos_yes=Position("yes-token", 1.0, 0.4))  # below min_order_size
    )
    assert not [q for q in tq.quotes if q.side == Side.SELL]


# ── spread widening ──────────────────────────────────────────────────────────


def test_toxicity_widens_spread_or_cuts_size(meta, profile):
    """Higher toxicity must worsen the quote: wider (lower) bid and/or smaller size.

    When the book best bid pins the join price, size is the free lever.
    """
    calm = construct_quotes(_inputs(meta, profile, regime=Regime.TRENDING, toxicity=0.0))
    toxic = construct_quotes(_inputs(meta, profile, regime=Regime.TRENDING, toxicity=0.02))

    def yes_buys(tq):
        return [q for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY]

    c, t = yes_buys(calm), yes_buys(toxic)
    assert c and t
    c_top, t_top = max(q.price for q in c), max(q.price for q in t)
    c_sz, t_sz = sum(q.size for q in c), sum(q.size for q in t)
    assert t_top < c_top or t_sz < c_sz


def test_quiet_regime_clamps_spread_to_reward_band(meta, profile):
    """In QUIET, even with high vol the bid stays within the reward band of FV."""
    tq = construct_quotes(_inputs(meta, profile, regime=Regime.QUIET, vol_short=0.5))
    band = meta.rewards_max_spread / 100.0  # 0.03
    top_yes = max(q.price for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY)
    # bid should be within (band + a tick of rounding) of FV
    assert top_yes >= 0.50 - band - meta.tick_size


def test_place_bid_join_best_bid_improves_from_below():
    """Opt-in join_best_bid raises a below-touch target up to best_bid when safe."""
    v = view(0.49, 0.51)  # touch at 0.49; FV 0.50 → edge_cap with min_edge=0 is 0.50
    # target below touch
    p_default = _place_bid(0.47, v, 0.01, 2, 0.50, 0, join_best_bid=False)
    assert p_default == 0.47
    p_join = _place_bid(0.47, v, 0.01, 2, 0.50, 0, join_best_bid=True)
    assert p_join == 0.49


def test_place_bid_join_best_bid_respects_min_edge():
    """Cannot join touch above FV−min_edge."""
    v = view(0.50, 0.52)  # BB at FV
    # min_edge=1 tick → edge_cap=0.49; BB=0.50 > edge_cap → stay at target
    p = _place_bid(0.47, v, 0.01, 2, 0.50, 1, join_best_bid=True)
    assert p == 0.47


def test_strategy_profile_join_best_bid_default_off():
    from polymaker.config import StrategyProfile

    assert StrategyProfile().join_best_bid is False


    assert isinstance(StrategyProfile().c_kyle, float)



def test_c_kyle_widens_half_spread_when_lambda_positive(meta, profile):
    """c_kyle>0 with kyle_lambda>0 should not tighten bids vs c_kyle=0."""
    from polymaker.config import StrategyProfile

    calm = construct_quotes(
        _inputs(meta, StrategyProfile(c_kyle=0.0), kyle_lambda=0.01)
    )
    wide = construct_quotes(
        _inputs(meta, StrategyProfile(c_kyle=2.0), kyle_lambda=0.01)
    )

    def top_yes_bid(tq):
        buys = [q.price for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY]
        return max(buys) if buys else None

    c_bid, w_bid = top_yes_bid(calm), top_yes_bid(wide)
    assert c_bid is not None and w_bid is not None
    assert w_bid <= c_bid + 1e-12


def test_entry_bids_never_dust_oob_on_junk_book(meta, profile):
    """Production path must not post 0.001 dust bids when best bid is junk.

    Livecfg regression: simple construct_quotes joined far best-bids and layers
    stepped out of the reward band → zero reward score.
    """
    from tests.conftest import view as _view

    junk = _view(0.001, 0.999)  # best bid dust, wide ask
    tq = construct_quotes(_inputs(
        meta, profile, regime=Regime.QUIET, fv=0.50, vol_short=0.01,
        yes_view=junk, no_view=junk,
    ))
    band = meta.rewards_max_spread / 100.0
    buys = [q for q in tq.quotes if q.side == Side.BUY]
    assert buys, "expected at least one in-band entry bid"
    for q in buys:
        # no dust
        assert q.price > 0.01
        # YES and NO must sit within reward band of their token FV
        tok_fv = 0.50  # both sides at mid for this fixture
        assert abs(q.price - tok_fv) <= band + meta.tick_size + 1e-9


def test_zero_inventory_quotes_both_entry_sides(meta, profile):
    """Documented edge: flat book → BUY YES and BUY NO (no exits)."""
    tq = construct_quotes(_inputs(meta, profile))
    assert not [q for q in tq.quotes if q.side == Side.SELL]
    assert {q.token_id for q in tq.quotes if q.side == Side.BUY} >= {"yes-token", "no-token"}


def test_max_inventory_pulls_adding_side(meta, profile):
    """When utilization >= q_soft_frac, stop adding YES (long) / NO (short)."""
    # defaults: q_max_usdc=500, fv=0.5 → q_max_shares=1000; soft 0.6 → 600 shares
    tq = construct_quotes(
        _inputs(meta, profile, pos_yes=Position("yes-token", 600, 0.5), vol_short=0.01)
    )
    yes_buys = [q for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY]
    no_buys = [q for q in tq.quotes if q.token_id == "no-token" and q.side == Side.BUY]
    assert yes_buys == []
    assert no_buys  # still bid the offsetting leg


def test_missing_book_view_does_not_crash(meta, profile):
    """Missing market data: empty BookView — construct_quotes must not raise."""
    empty = view(None, None)
    tq = construct_quotes(_inputs(meta, profile, yes_view=empty, no_view=empty))
    assert tq.condition_id == meta.condition_id
    assert isinstance(tq.quotes, tuple)


def test_sell_quotes_when_inventory_present(meta, profile):
    """When inventory is present, SELL quotes are placed to exit."""
    tq = construct_quotes(
        _inputs(meta, profile, pos_yes=Position("yes-token", 50, 0.5))
    )
    sells = [q for q in tq.quotes if q.side == Side.SELL]
    assert sells, "expected SELL quotes when YES inventory present"
    for q in sells:
        assert q.token_id == "yes-token"
        assert q.price > 0.5  # SELL above mid


def test_both_sides_quoted_with_inventory_on_both(meta, profile):
    """When both YES and NO inventory, both SELL sides are quoted."""
    tq = construct_quotes(
        _inputs(
            meta, profile,
            pos_yes=Position("yes-token", 50, 0.5),
            pos_no=Position("no-token", 50, 0.5),
        )
    )
    yes_sells = [q for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.SELL]
    no_sells = [q for q in tq.quotes if q.token_id == "no-token" and q.side == Side.SELL]
    assert yes_sells, "expected YES SELL quotes"
    assert no_sells, "expected NO SELL quotes"


def test_max_open_orders_caps_accumulation(meta, profile):
    """max_open_orders_per_market prevents order book accumulation."""
    from polymaker.config import StrategyProfile
    capped = StrategyProfile(
        base_size_usdc=profile.base_size_usdc,
        q_max_usdc=profile.q_max_usdc,
        layers=5,
        layer_step_ticks=profile.layer_step_ticks,
        max_open_orders_per_market=2,
    )
    tq = construct_quotes(_inputs(meta, capped))
    yes_orders = [q for q in tq.quotes if q.token_id == "yes-token"]
    assert len(yes_orders) <= 2, f"expected ≤2 YES orders, got {len(yes_orders)}"


def test_zero_inventory_no_sells(meta, profile):
    """With zero inventory, no SELL orders (only entries/exits from inventory)."""
    tq = construct_quotes(_inputs(meta, profile))
    sells = [q for q in tq.quotes if q.side == Side.SELL]
    assert sells == [], f"expected no SELL with zero inventory, got {sells}"
