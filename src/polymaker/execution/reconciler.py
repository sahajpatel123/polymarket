"""Pure reconciliation: desired TargetQuotes vs live orders -> minimal actions.

The strategy emits a target quote set; this computes the smallest cancel/place
set to reach it, applying churn tolerances so we don't burn queue position for
sub-tick or sub-threshold size changes (v1's should_cancel instinct, generalized).
No I/O — the gateway executes the returned plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polymaker.domain import OpenOrder, Quote, TargetQuotes

_EPS = 1e-9

# Above this many live orders, the defaultdict index pays for its allocation
# cost; below it, a linear scan is faster (avoids dict/set construction).
_LINEAR_SCAN_THRESHOLD = 8


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    to_cancel: list[str] = field(default_factory=list)  # order ids
    to_place: list[Quote] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.to_cancel and not self.to_place


def reconcile(
    targets: TargetQuotes,
    live: list[OpenOrder],
    *,
    tick: float,
    reprice_ticks: int,
    resize_frac: float,
) -> ReconcilePlan:
    """Diff targets against live orders. Keep live orders that already satisfy a
    target within tolerance; cancel the rest; place targets with no match."""
    keep: set[str] = set()
    to_place: list[Quote] = []
    price_tol = reprice_ticks * tick + _EPS

    if len(live) <= _LINEAR_SCAN_THRESHOLD:
        # Fast path: small live set — linear scan avoids dict/set allocation.
        # Output is identical to the indexed path; only the lookup strategy
        # differs.
        for q in targets.quotes:
            match: OpenOrder | None = None
            for o in live:
                if o.order_id in keep:
                    continue
                if o.token_id == q.token_id and o.side == q.side:
                    price_close = abs(o.price - q.price) <= price_tol
                    size_close = q.size <= 0 or abs(o.size - q.size) <= resize_frac * q.size + _EPS
                    if price_close and size_close:
                        match = o
                        break
            if match is not None:
                keep.add(match.order_id)
            else:
                to_place.append(q)
    else:
        # Indexed path: group live orders by (token_id, side) for O(1) lookup.
        live_by_key: dict[tuple[str, str], list[OpenOrder]] = {}
        for o in live:
            live_by_key.setdefault((o.token_id, o.side.value), []).append(o)

        for q in targets.quotes:
            candidates = live_by_key.get((q.token_id, q.side.value), [])
            match = None
            for o in candidates:
                if o.order_id in keep:
                    continue
                price_close = abs(o.price - q.price) <= price_tol
                size_close = q.size <= 0 or abs(o.size - q.size) <= resize_frac * q.size + _EPS
                if price_close and size_close:
                    match = o
                    break
            if match is not None:
                keep.add(match.order_id)
            else:
                to_place.append(q)

    to_cancel = [o.order_id for o in live if o.order_id not in keep]
    return ReconcilePlan(to_cancel=to_cancel, to_place=to_place)
