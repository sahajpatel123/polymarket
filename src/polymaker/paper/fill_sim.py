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
removed. Multiple orders at the same price level fill price-priority then
in placement order.

No I/O — pure state machine fed trade prints.

Invariants (callers must keep external live-order maps in sync):
  - cancel → order never fills afterwards
  - remaining size after fills matches sim state
  - one trade cannot fill more volume than aggressor size
  - duplicate trade_ids are the caller's responsibility (StateStore)
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
    original_size: float = 0.0


class FillSimulator:
    """Tracks resting orders and matches them against trade prints.

    Used by paper mode and the replay backtester. Live mode does not use
    this — real fills arrive via the user WS.
    """

    def __init__(self) -> None:
        self._orders: dict[str, _Resting] = {}  # order_id -> resting
        self._fill_seq: int = 0

    def place(self, order: OpenOrder, **_kwargs: object) -> None:
        """Register a newly placed order for matching.

        Extra kwargs (e.g. ts=, queue_ahead=) are ignored — accepted so
        callers can use the same place() signature as queue-aware sims.
        """
        self._orders[order.order_id] = _Resting(
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            original_size=order.size,
        )

    def cancel(self, order_id: str) -> None:
        """Remove a cancelled order from the matching set."""
        self._orders.pop(order_id, None)

    def remaining(self, order_id: str) -> float:
        """Remaining size for an order, or 0 if unknown/gone."""
        op = self._orders.get(order_id)
        return op.size if op is not None else 0.0

    def order_ids(self) -> set[str]:
        return set(self._orders.keys())

    def remaining_map(self) -> dict[str, float]:
        return {oid: op.size for oid, op in self._orders.items()}

    def match(
        self,
        tp_asset_id: str,
        aggressor: Side,
        price: float,
        size: float,
        ts: float,
    ) -> list[Fill]:
        """Match a trade print against resting orders. Returns generated fills.

        For a SELL aggressor (hitting bids): match against BUY orders at
        price >= the trade price. For a BUY aggressor (lifting asks): match
        against SELL orders at price <= the trade price.

        Consumes at most `size` shares of resting liquidity (price priority).
        Each Fill carries order_id so callers can sync live order state.
        """
        if size <= 0:
            return []

        if aggressor is Side.BUY:
            target_side = Side.SELL

            def price_ok(op: _Resting) -> bool:
                return op.price <= price
        else:
            target_side = Side.BUY

            def price_ok(op: _Resting) -> bool:
                return op.price >= price

        candidates = [
            (oid, op)
            for oid, op in self._orders.items()
            if op.token_id == tp_asset_id
            and op.side == target_side
            and op.size > 0
            and price_ok(op)
        ]
        if not candidates:
            return []

        if target_side is Side.BUY:
            candidates.sort(key=lambda x: (-x[1].price, x[0]))
        else:
            candidates.sort(key=lambda x: (x[1].price, x[0]))

        fills: list[Fill] = []
        remaining = size
        for oid, op in candidates:
            if remaining <= 0:
                break
            fill_size = min(op.size, remaining)
            self._fill_seq += 1
            fills.append(
                Fill(
                    token_id=op.token_id,
                    side=op.side,
                    price=op.price,
                    size=fill_size,
                    trade_id=f"paper-fill-{ts:.6f}-{oid}-{self._fill_seq}",
                    ts=ts,
                    is_maker=True,
                    order_id=oid,
                )
            )
            op.size -= fill_size
            remaining -= fill_size
            if op.size <= 1e-12:
                del self._orders[oid]

        # Invariant: never fill more than aggressor size
        assert sum(f.size for f in fills) <= size + 1e-9
        return fills

    def orders_for(self, token_id: str) -> list[OpenOrder]:
        """Return current resting orders for a token as OpenOrders."""
        out: list[OpenOrder] = []
        for oid, op in self._orders.items():
            if op.token_id == token_id:
                out.append(
                    OpenOrder(
                        oid, op.token_id, op.side, op.price, op.size, OrderState.LIVE
                    )
                )
        return out

    def all_orders(self) -> list[OpenOrder]:
        """Return all resting orders as OpenOrders."""
        out: list[OpenOrder] = []
        for oid, op in self._orders.items():
            out.append(
                OpenOrder(oid, op.token_id, op.side, op.price, op.size, OrderState.LIVE)
            )
        return out

    def clear(self) -> None:
        """Remove all resting orders."""
        self._orders.clear()
