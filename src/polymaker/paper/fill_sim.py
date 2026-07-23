"""Fill simulation for paper mode and backtesting.

When a resting order would be crossed by an aggressor trade print, this
module generates a Fill event so the strategy can track inventory, PnL,
and toxicity (markout) — the same path live trading uses via the user WS.

Matching rule (post-only maker semantics):
  - BUY  order at price P is filled by a SELL aggressor at price <= P
    (aggressor hits the bid; we sell into them at our bid price)
  - SELL order at price P is filled by a BUY aggressor at price >= P
    (aggressor lifts the ask; we sell at our ask price)

Partial fills reduce the order's remaining size; fully-filled orders are
removed. Multiple orders at the same price level fill pro-rata.

No I/O — pure state machine fed trade prints.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.domain import Fill, OpenOrder, OrderState, Side


@dataclass(slots=True)
class _Resting:
    """Internal copy of a resting order for matching."""

    token_id: str
    side: Side
    price: float
    size: float  # remaining


class FillSimulator:
    """Tracks resting orders and matches them against trade prints.

    Used by paper mode and the replay backtester. Live mode does not use
    this — real fills arrive via the user WS.
    """

    def __init__(self) -> None:
        self._orders: dict[str, _Resting] = {}  # order_id -> resting

    def place(self, order: OpenOrder) -> None:
        """Register a newly placed order for matching."""
        self._orders[order.order_id] = _Resting(
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
        )

    def cancel(self, order_id: str) -> None:
        """Remove a cancelled order from the matching set."""
        self._orders.pop(order_id, None)

    def match(self, tp_asset_id: str, aggressor: Side, price: float, size: float, ts: float) -> list[Fill]:
        """Match a trade print against resting orders. Returns generated fills.

        For a SELL aggressor (hitting bids): match against BUY orders at
        price >= the trade price. For a BUY aggressor (lifting asks): match
        against SELL orders at price <= the trade price.

        Consumes `size` shares of resting liquidity across all matching
        orders (pro-rata within the same price level, best-price-first).
        """
        if size <= 0:
            return []

        # Determine which side of our book the aggressor consumes.
        if aggressor is Side.BUY:
            # BUY aggressor lifts asks: match against our SELL orders at price <= trade price
            target_side = Side.SELL
            def price_ok(op: _Resting) -> bool:
                return op.price <= price
        else:
            # SELL aggressor hits bids: match against our BUY orders at price >= trade price
            target_side = Side.BUY
            def price_ok(op: _Resting) -> bool:
                return op.price >= price

        # Find matching orders, sorted by price priority:
        # BUY side: highest price first (we get picked off at worst price)
        # SELL side: lowest price first (we get picked off at worst price)
        candidates = [
            (oid, op) for oid, op in self._orders.items()
            if op.token_id == tp_asset_id and op.side == target_side and op.size > 0 and price_ok(op)
        ]
        if not candidates:
            return []

        if target_side is Side.BUY:
            # We're selling; aggressor hits our bids. Best for aggressor = highest bid.
            candidates.sort(key=lambda x: x[1].price, reverse=True)
        else:
            # We're buying; aggressor lifts our asks. Best for aggressor = lowest ask.
            candidates.sort(key=lambda x: x[1].price)

        fills: list[Fill] = []
        remaining = size
        for oid, op in candidates:
            if remaining <= 0:
                break
            fill_size = min(op.size, remaining)
            fills.append(Fill(
                token_id=op.token_id,
                side=op.side,  # our side (BUY = we bought, SELL = we sold)
                price=op.price,
                size=fill_size,
                trade_id=f"paper-fill-{ts:.6f}-{oid[:8]}",
                ts=ts,
                is_maker=True,
            ))
            op.size -= fill_size
            remaining -= fill_size
            if op.size <= 0:
                del self._orders[oid]

        return fills

    def orders_for(self, token_id: str) -> list[OpenOrder]:
        """Return current resting orders for a token as OpenOrders."""
        out: list[OpenOrder] = []
        for oid, op in self._orders.items():
            if op.token_id == token_id:
                out.append(OpenOrder(oid, op.token_id, op.side, op.price, op.size, OrderState.LIVE))
        return out

    def all_orders(self) -> list[OpenOrder]:
        """Return all resting orders as OpenOrders."""
        out: list[OpenOrder] = []
        for oid, op in self._orders.items():
            out.append(OpenOrder(oid, op.token_id, op.side, op.price, op.size, OrderState.LIVE))
        return out

    def clear(self) -> None:
        """Remove all resting orders."""
        self._orders.clear()
