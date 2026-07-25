"""Promotion-grade fill models: optimistic / base / conservative.

optimistic  — current cross-only FillSimulator (upper bound; never promotion gate)
base        — visible queue ahead + time priority; partial fills
conservative — large queue ahead assumption + latency; ambiguous trades skip

Financial claims must pass under conservative (or at least base).
Winning only under optimistic is not enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from polymaker.domain import Fill, OpenOrder, OrderState, Side
from polymaker.paper.fill_sim import FillSimulator


class FillMode(str, Enum):
    OPTIMISTIC = "optimistic"
    BASE = "base"
    CONSERVATIVE = "conservative"


@dataclass(slots=True)
class _QOrder:
    token_id: str
    side: Side
    price: float
    size: float
    placed_ts: float
    queue_ahead: float  # shares assumed ahead at placement


class QueueAwareFillSimulator:
    """Queue-aware maker fill simulator for base/conservative modes.

    Does not inherit FillSimulator matching (which is optimistic). Implements
    the same place/cancel/match/remaining API so replay can swap modes.
    """

    def __init__(
        self,
        *,
        mode: FillMode = FillMode.BASE,
        default_queue_ahead: float | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.mode = mode if isinstance(mode, FillMode) else FillMode(str(mode))
        self._orders: dict[str, _QOrder] = {}
        self._fill_seq = 0
        self.latency_s = float(latency_s)
        # Default queue ahead if not provided at place time
        if default_queue_ahead is not None:
            self._default_ahead = float(default_queue_ahead)
        elif self.mode is FillMode.CONSERVATIVE:
            self._default_ahead = 200.0  # large visible book ahead
        elif self.mode is FillMode.BASE:
            self._default_ahead = 50.0
        else:
            self._default_ahead = 0.0
        self.n_queue_blocked = 0
        self.n_latency_blocked = 0

    def place(self, order: OpenOrder, *, queue_ahead: float | None = None, ts: float | None = None) -> None:
        ahead = self._default_ahead if queue_ahead is None else float(queue_ahead)
        if self.mode is FillMode.OPTIMISTIC:
            ahead = 0.0
        self._orders[order.order_id] = _QOrder(
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            size=order.size,
            placed_ts=float(ts if ts is not None else order.created_ts),
            queue_ahead=max(0.0, ahead),
        )

    def cancel(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def remaining(self, order_id: str) -> float:
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
        if size <= 0:
            return []

        if aggressor is Side.BUY:
            target_side = Side.SELL

            def price_ok(op: _QOrder) -> bool:
                return op.price <= price
        else:
            target_side = Side.BUY

            def price_ok(op: _QOrder) -> bool:
                return op.price >= price

        candidates = [
            (oid, op)
            for oid, op in self._orders.items()
            if op.token_id == tp_asset_id
            and op.side is target_side
            and op.size > 0
            and price_ok(op)
        ]
        if not candidates:
            return []

        if target_side is Side.BUY:
            candidates.sort(key=lambda x: (-x[1].price, x[1].placed_ts, x[0]))
        else:
            candidates.sort(key=lambda x: (x[1].price, x[1].placed_ts, x[0]))

        fills: list[Fill] = []
        remaining = size
        for oid, op in candidates:
            if remaining <= 0:
                break
            # Latency: order not live yet
            if self.latency_s > 0 and (ts - op.placed_ts) < self.latency_s:
                self.n_latency_blocked += 1
                continue
            # Queue: trade must eat through size ahead first
            if op.queue_ahead > 0:
                if remaining <= op.queue_ahead:
                    # Entire trade absorbed by queue ahead
                    op.queue_ahead -= remaining
                    remaining = 0.0
                    self.n_queue_blocked += 1
                    break
                remaining -= op.queue_ahead
                op.queue_ahead = 0.0

            # Conservative: require clear cross by at least 1 tick-ish (0.001)
            if self.mode is FillMode.CONSERVATIVE:
                if aggressor is Side.SELL and price > op.price - 1e-12:
                    # SELL aggressor must hit through our bid (price < our bid)
                    # price_ok already requires price <= our bid for BUY target
                    # Ambiguous equal-price: skip in conservative
                    if abs(price - op.price) < 1e-12:
                        self.n_queue_blocked += 1
                        continue
                if aggressor is Side.BUY and abs(price - op.price) < 1e-12:
                    self.n_queue_blocked += 1
                    continue

            fill_size = min(op.size, remaining)
            if fill_size <= 0:
                continue
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

        assert sum(f.size for f in fills) <= size + 1e-9
        return fills

    def orders_for(self, token_id: str) -> list[OpenOrder]:
        out: list[OpenOrder] = []
        for oid, op in self._orders.items():
            if op.token_id == token_id:
                out.append(
                    OpenOrder(oid, op.token_id, op.side, op.price, op.size, OrderState.LIVE)
                )
        return out

    def all_orders(self) -> list[OpenOrder]:
        return [
            OpenOrder(oid, op.token_id, op.side, op.price, op.size, OrderState.LIVE)
            for oid, op in self._orders.items()
        ]

    def clear(self) -> None:
        self._orders.clear()


def make_fill_simulator(
    mode: str | FillMode = FillMode.OPTIMISTIC,
    *,
    latency_s: float = 0.0,
    default_queue_ahead: float | None = None,
) -> FillSimulator | QueueAwareFillSimulator:
    """Factory: optimistic → classic FillSimulator; base/conservative → queue-aware."""
    m = mode if isinstance(mode, FillMode) else FillMode(str(mode))
    if m is FillMode.OPTIMISTIC:
        return FillSimulator()
    return QueueAwareFillSimulator(
        mode=m,
        default_queue_ahead=default_queue_ahead,
        latency_s=latency_s if m is FillMode.CONSERVATIVE else latency_s,
    )
