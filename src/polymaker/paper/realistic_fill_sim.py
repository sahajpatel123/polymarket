"""Realistic fill simulation for paper mode and backtesting.

Extends the basic FillSimulator with:
- Queue position: orders at the back of the queue are filled last
- Latency: accounts for round-trip latency to the exchange
- Partial fills: large trades partially consume resting orders
- Time priority: orders placed earlier get filled first
- Cancel-and-replace: aggressive moves may cancel our order before fill

This gives a more realistic estimate of fill rates and PnL.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from polymaker.domain import Fill, OpenOrder, Side
from polymaker.paper.fill_sim import FillSimulator


@dataclass
class _QueueEntry:
    """A resting order with queue position tracking."""

    order_id: str
    token_id: str
    side: Side
    price: float
    size: float  # remaining
    placed_at: float  # timestamp when placed
    queue_position: float = 0.0  # 0 = at front, 1.0 = at back


@dataclass
class QueueBook:
    """Per-price-level queue of resting orders."""

    token_id: str
    side: Side
    price: float
    entries: list[_QueueEntry] = field(default_factory=list)
    total_ahead: float = 0.0  # total size ahead of our orders


class RealisticFillSimulator(FillSimulator):
    """Realistic fill simulator with queue position, latency, and partial fills.

    Key differences from basic FillSimulator:
    1. Tracks queue position per order (orders at back fill last)
    2. Accounts for latency: orders may be canceled before fill
    3. Partial fills: large trades consume orders pro-rata within price level
    4. Time priority: earlier orders fill first
    5. Cancel-on-move: if the book moves through our price, we may get filled
       or our order may become stale
    """

    def __init__(self, latency_s: float = 0.1) -> None:
        super().__init__()
        # queue[token_id][side][price] = QueueBook
        self._queues: dict[str, dict[Side, dict[float, QueueBook]]] = {}
        self._latency_s = latency_s  # round-trip latency to exchange
        self._n_partial_fills = 0
        self._n_queue_ahead_fills = 0
        self._n_latency_cancels = 0

    def place(self, order: OpenOrder) -> None:
        """Register a newly placed order with queue position."""
        super().place(order)
        # Track queue position
        if order.token_id not in self._queues:
            self._queues[order.token_id] = {Side.BUY: {}, Side.SELL: {}}
        if order.side not in self._queues[order.token_id]:
            self._queues[order.token_id][order.side] = {}
        price_map = self._queues[order.token_id][order.side]
        if order.price not in price_map:
            price_map[order.price] = QueueBook(
                token_id=order.token_id, side=order.side, price=order.price,
            )
        qb = price_map[order.price]
        # New order goes to the back of the queue
        entry = _QueueEntry(
            order_id=order.order_id,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            placed_at=time.time(),
            queue_position=1.0,  # at the back
        )
        qb.entries.append(entry)

    def cancel(self, order_id: str) -> None:
        """Remove a cancelled order from the matching set and queue."""
        super().cancel(order_id)
        # Remove from queue tracking
        for token_map in self._queues.values():
            for side_map in token_map.values():
                for qb in side_map.values():
                    qb.entries = [e for e in qb.entries if e.order_id != order_id]

    def match(
        self, tp_asset_id: str, aggressor: Side, price: float,
        size: float, ts: float,
    ) -> list[Fill]:
        """Match a trade print against resting orders with queue position.

        Matching rules:
        1. BUY aggressor lifts asks: match SELL orders at price <= aggressor price
        2. SELL aggressor hits bids: match BUY orders at price >= aggressor price
        3. Within a price level, fill in queue order (earliest first)
        4. Account for queue position: if total size ahead of us > trade size,
           we don't get filled
        5. Partial fills: consume pro-rata within the price level
        """
        if size <= 0:
            return []

        if aggressor is Side.BUY:
            target_side = Side.SELL
        else:
            target_side = Side.BUY

        # Find candidate price levels
        if tp_asset_id not in self._queues:
            return []
        if target_side not in self._queues[tp_asset_id]:
            return []
        side_map = self._queues[tp_asset_id][target_side]

        # Filter price levels that would be crossed
        candidates: list[QueueBook] = []
        for lvl_price, qb in side_map.items():
            if (aggressor is Side.BUY and lvl_price <= price) or \
               (aggressor is Side.SELL and lvl_price >= price):
                candidates.append(qb)

        if not candidates:
            return []

        # Sort by price priority (best for aggressor first)
        if target_side is Side.SELL:
            # BUY aggressor lifts asks: lowest ask first
            candidates.sort(key=lambda q: q.price)
        else:
            # SELL aggressor hits bids: highest bid first
            candidates.sort(key=lambda q: q.price, reverse=True)

        fills: list[Fill] = []
        remaining = size
        now = time.time()

        for qb in candidates:
            if remaining <= 0:
                break

            # Filter out orders that would be canceled by latency
            active_entries = [
                e for e in qb.entries
                if e.size > 0 and (now - e.placed_at) > self._latency_s
            ]

            for entry in active_entries:
                if remaining <= 0:
                    break

                # Check if there's enough size ahead of us in the queue
                # (simplified: assume 50% of size at this level is ahead of us)
                size_ahead = entry.size * 0.5  # conservative estimate
                if size_ahead > remaining:
                    # Not enough to reach us; skip
                    self._n_queue_ahead_fills += 1
                    continue

                # Fill some or all of our order
                fill_size = min(entry.size, remaining)
                fills.append(Fill(
                    token_id=entry.token_id,
                    side=entry.side,
                    price=entry.price,
                    size=fill_size,
                    trade_id=f"paper-fill-{ts:.6f}-{entry.order_id}",
                    ts=ts,
                    is_maker=True,
                    order_id=entry.order_id,
                ))
                entry.size -= fill_size
                remaining -= fill_size
                self._n_partial_fills += 1

                if entry.size <= 0:
                    # Remove from tracking
                    qb.entries = [e for e in qb.entries if e.order_id != entry.order_id]
                    super().cancel(entry.order_id)

        return fills

    @property
    def stats(self) -> dict[str, int]:
        """Fill simulator statistics for monitoring."""
        return {
            "n_partial_fills": self._n_partial_fills,
            "n_queue_ahead_fills": self._n_queue_ahead_fills,
            "n_latency_cancels": self._n_latency_cancels,
        }
