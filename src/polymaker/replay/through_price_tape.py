"""Classify sell aggressors vs book best bid (through / at-touch / above).

Conservative fill mode only credits maker bids when the sell print is
strictly through the bid (trade_price < bid). Join-BB fills are at-touch
(equal price) and are skipped. This diagnostic asks whether the *tape*
even contains through-price sells — if not, join-touch cannot promote
under the current conservative model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polymaker.domain import MarketMeta, Side
from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_last_trade, parse_price_changes


@dataclass(frozen=True)
class ThroughPriceTape:
    n_sell: int
    n_buy: int
    n_sell_with_bb: int
    n_through: int  # trade < best_bid
    n_at_touch: int  # trade == best_bid
    n_above_touch: int  # trade > best_bid (doesn't hit bid)
    frac_through: float
    frac_at_touch: float
    mean_trade_minus_bb: float | None
    tick_size: float
    reason: str = ""

    @property
    def conservative_join_viable(self) -> bool:
        return self.n_through > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_sell": self.n_sell,
            "n_buy": self.n_buy,
            "n_sell_with_bb": self.n_sell_with_bb,
            "n_through": self.n_through,
            "n_at_touch": self.n_at_touch,
            "n_above_touch": self.n_above_touch,
            "frac_through": round(self.frac_through, 6),
            "frac_at_touch": round(self.frac_at_touch, 6),
            "mean_trade_minus_bb": (
                None
                if self.mean_trade_minus_bb is None
                else round(self.mean_trade_minus_bb, 6)
            ),
            "tick_size": self.tick_size,
            "reason": self.reason,
            "conservative_join_viable": self.conservative_join_viable,
        }


def _book_for(books: dict[str, OrderBook], asset_id: str, tick: float) -> OrderBook:
    if asset_id not in books:
        books[asset_id] = OrderBook(tick_size=tick)
    return books[asset_id]


def measure_through_price_tape(
    rows: list[dict[str, Any]],
    meta: MarketMeta,
) -> ThroughPriceTape:
    """Walk journal books; classify SELL aggressors vs contemporaneous best bid."""
    tick = float(meta.tick_size or 0.01)
    wanted = {meta.yes.token_id, meta.no.token_id}
    books: dict[str, OrderBook] = {}
    n_sell = n_buy = 0
    n_with_bb = 0
    n_through = n_at = n_above = 0
    gaps: list[float] = []

    for row in rows:
        kind = str(row.get("kind") or "")
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        if kind == "book":
            bu = parse_book(data)
            if bu is None or bu.asset_id not in wanted:
                continue
            book = _book_for(books, bu.asset_id, tick)
            if bu.tick_size:
                book.set_tick_size(bu.tick_size)
            book.apply_snapshot(bu.bids, bu.asks, bu.ts, bu.book_hash)
            continue
        if kind == "price_change":
            for pc in parse_price_changes(data):
                if pc.asset_id not in wanted:
                    continue
                book = _book_for(books, pc.asset_id, tick)
                book.apply_delta(pc.side, pc.price, pc.size, pc.ts)
            continue
        if kind != "last_trade_price":
            continue
        tp = parse_last_trade(data)
        if tp is None or tp.asset_id not in wanted:
            continue
        if tp.aggressor is Side.BUY:
            n_buy += 1
            continue
        n_sell += 1
        book = books.get(tp.asset_id)
        if book is None:
            continue
        view = book.view()
        if view.best_bid is None:
            continue
        n_with_bb += 1
        bb = float(view.best_bid)
        px = float(tp.price)
        gap = px - bb
        gaps.append(gap)
        if px < bb - 1e-12:
            n_through += 1
        elif abs(px - bb) <= 1e-12:
            n_at += 1
        else:
            n_above += 1

    denom = max(n_with_bb, 1)
    mean_gap = (sum(gaps) / len(gaps)) if gaps else None
    reasons: list[str] = []
    if n_sell == 0:
        reasons.append("no_sell_aggressors")
    elif n_with_bb == 0:
        reasons.append("no_book_best_bid_at_trade")
    elif n_through == 0 and n_at > 0:
        reasons.append("sells_at_touch_only_no_through")
    elif n_through == 0:
        reasons.append("no_through_price_sells")
    else:
        reasons.append("ok_has_through")

    return ThroughPriceTape(
        n_sell=n_sell,
        n_buy=n_buy,
        n_sell_with_bb=n_with_bb,
        n_through=n_through,
        n_at_touch=n_at,
        n_above_touch=n_above,
        frac_through=n_through / denom,
        frac_at_touch=n_at / denom,
        mean_trade_minus_bb=mean_gap,
        tick_size=tick,
        reason=";".join(reasons),
    )
