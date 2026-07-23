"""Deterministic journal replay through the pure strategy stack.

Reads Journal JSONL (`book` / `price_change` / `last_trade_price`) and drives
OrderBook + estimators + RegimeMachine + construct_quotes + reconcile, emitting
the same MetricsLogger events as live/paper (T1-01). No network I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, OpenOrder, Position, Side
from polymaker.execution.reconciler import reconcile
from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import (
    TradePrint,
    parse_book,
    parse_last_trade,
    parse_price_changes,
    parse_tick_size_change,
)
from polymaker.metrics import MetricsLogger, inventory_fields
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.quoting import QuoteInputs, compute_fair_value, construct_quotes
from polymaker.strategy.regime import RegimeInputs, RegimeMachine


@dataclass
class ReplayResult:
    events_read: int = 0
    events_applied: int = 0
    recomputes: int = 0
    metrics_path: str = ""
    n_quote: int = 0
    n_cancel: int = 0
    n_mark: int = 0


@dataclass
class ReplayState:
    meta: MarketMeta
    profile: StrategyProfile
    yes_book: OrderBook = field(default_factory=OrderBook)
    no_book: OrderBook = field(default_factory=OrderBook)
    est: MarketEstimators | None = None
    regime: RegimeMachine = field(default_factory=RegimeMachine)
    live: dict[str, OpenOrder] = field(default_factory=dict)
    pos_yes: Position = field(default_factory=lambda: Position("yes"))
    pos_no: Position = field(default_factory=lambda: Position("no"))
    metrics: MetricsLogger | None = None
    n_quote: int = 0
    n_cancel: int = 0
    n_mark: int = 0
    recomputes: int = 0

    def __post_init__(self) -> None:
        self.pos_yes = Position(self.meta.yes.token_id)
        self.pos_no = Position(self.meta.no.token_id)
        self.yes_book = OrderBook(tick_size=self.meta.tick_size)
        self.no_book = OrderBook(tick_size=self.meta.tick_size)
        p = self.profile
        self.est = MarketEstimators(
            vol=VolEstimator(p.vol_short_halflife_s, p.vol_long_halflife_s),
            flow=FlowEstimator(p.flow_ewma_halflife_s),
            markout=MarkoutTracker(),
        )


def load_journal(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and "kind" in obj:
                rows.append(obj)
    rows.sort(key=lambda r: float(r.get("ts") or 0.0))
    return rows


def filter_rows_for_tokens(
    rows: list[dict[str, Any]],
    *,
    yes_token: str,
    no_token: str,
) -> list[dict[str, Any]]:
    """Keep journal rows that touch the given YES/NO token ids.

    Multi-market journals otherwise poison time/event holdout splits: the last
    30% of rows may be almost entirely a different market.
    """
    wanted = {yes_token, no_token}
    out: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        data = row.get("data")
        if kind == "book" and isinstance(data, dict):
            if str(data.get("asset_id") or "") in wanted:
                out.append(row)
            continue
        if kind == "last_trade_price" and isinstance(data, dict):
            if str(data.get("asset_id") or "") in wanted:
                out.append(row)
            continue
        if kind == "tick_size_change" and isinstance(data, dict):
            if str(data.get("asset_id") or "") in wanted:
                out.append(row)
            continue
        if kind == "price_change" and isinstance(data, dict):
            changes = data.get("price_changes") or []
            if any(str(ch.get("asset_id") or "") in wanted for ch in changes if isinstance(ch, dict)):
                # Keep row; apply_journal_event ignores non-matching assets.
                out.append(row)
            continue
        # drop orders_out / unknown
    return out


def infer_yes_no_tokens(
    metrics_path: Path,
    condition_id: str,
) -> tuple[str, str] | None:
    """Infer YES/NO token ids from metrics quote prices (lower mean px = YES)."""
    means: dict[str, list[float]] = {}
    if not metrics_path.exists():
        return None
    with metrics_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("event") != "quote":
                continue
            if str(obj.get("condition_id") or "") != condition_id:
                continue
            tid = str(obj.get("token_id") or "")
            try:
                px = float(obj.get("price"))
            except (TypeError, ValueError):
                continue
            if not tid:
                continue
            means.setdefault(tid, []).append(px)
    if len(means) < 2:
        return None
    ranked = sorted(
        ((tid, sum(xs) / len(xs)) for tid, xs in means.items()),
        key=lambda kv: kv[1],
    )
    return ranked[0][0], ranked[1][0]


def discover_condition_ids(metrics_path: Path) -> list[str]:
    ids: set[str] = set()
    if not metrics_path.exists():
        return []
    with metrics_path.open() as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("event") in ("quote", "market_meta", "mark"):
                cid = obj.get("condition_id")
                if cid:
                    ids.add(str(cid))
    return sorted(ids)


def _empty_view():
    from polymaker.marketdata.orderbook import BookView

    return BookView(None, 0.0, None, 0.0, None, None, 0.0, 0.0)


def _recompute(st: ReplayState, now: float) -> None:
    assert st.est is not None and st.metrics is not None
    meta, p = st.meta, st.profile
    yb, nb = st.yes_book, st.no_book
    if yb.is_empty:
        return
    # Compute view once and reuse for crossed/locked check + construct_quotes,
    # avoiding redundant best_bid()/best_ask() calls (4992 saved per 5k replay).
    yes_view = yb.view()
    if yes_view.best_bid is None or yes_view.best_ask is None:
        return
    if yes_view.best_bid >= yes_view.best_ask:
        return

    micro = yb.microprice(p.micro_levels)
    if micro is None:
        return
    st.est.flow.decay_to(now)
    fv = compute_fair_value(micro, st.est.flow.z, meta.tick_size)
    prev_fv = st.est.last_fv
    st.est.on_fair_value(fv, now)

    q_max = p.q_max_usdc
    inv_util = (
        abs(st.pos_yes.size - st.pos_no.size) * fv / q_max if q_max > 0 else 0.0
    )
    regime = st.regime.decide(
        RegimeInputs(
            now=now,
            tick=meta.tick_size,
            fv=fv,
            prev_fv=prev_fv,
            vol_ratio=st.est.vol.ratio,
            flow_z=st.est.flow.z,
            inventory_util=inv_util,
            hours_to_end=None,
        ),
        p,
    )
    tq = construct_quotes(
        QuoteInputs(
            meta=meta,
            regime=regime,
            fv=fv,
            vol_short=st.est.vol.short,
            toxicity=st.est.markout.toxicity,
            yes_view=yes_view,
            no_view=nb.view() if not nb.is_empty else _empty_view(),
            pos_yes=st.pos_yes,
            pos_no=st.pos_no,
            profile=p,
            now=now,
        )
    )
    live = list(st.live.values())
    plan = reconcile(
        tq, live, tick=meta.tick_size, reprice_ticks=p.reprice_ticks, resize_frac=p.resize_frac
    )
    inv = inventory_fields(st.pos_yes.size, st.pos_no.size)
    st.metrics.emit(
        "mark", ts=now, condition_id=meta.condition_id, fv=fv, regime=regime.value, **inv
    )
    st.n_mark += 1
    st.recomputes += 1

    if plan.is_noop:
        return

    for oid in plan.to_cancel:
        o = st.live.pop(oid, None)
        if o is None:
            continue
        st.metrics.emit(
            "cancel",
            ts=now,
            condition_id=meta.condition_id,
            token_id=o.token_id,
            side=o.side.value,
            price=o.price,
            size=o.size,
            order_id=o.order_id,
            **inv,
        )
        st.n_cancel += 1

    reward_band = meta.rewards_max_spread / 100.0
    for i, q in enumerate(plan.to_place):
        oid = f"replay-{st.recomputes}-{i}"
        o = OpenOrder(oid, q.token_id, q.side, q.price, q.size)
        st.live[oid] = o
        mid_tok = fv if q.token_id == meta.yes.token_id else (1.0 - fv)
        in_band = reward_band > 0 and abs(q.price - mid_tok) <= reward_band
        st.metrics.emit(
            "quote",
            ts=now,
            condition_id=meta.condition_id,
            token_id=q.token_id,
            side=q.side.value,
            price=q.price,
            size=q.size,
            order_id=oid,
            mid=mid_tok,
            fv_yes=fv,
            in_reward_band=in_band,
            **inv,
        )
        st.n_quote += 1


def _book_for(st: ReplayState, token_id: str) -> OrderBook | None:
    if token_id == st.meta.yes.token_id:
        return st.yes_book
    if token_id == st.meta.no.token_id:
        return st.no_book
    return None


def apply_journal_event(st: ReplayState, row: dict[str, Any]) -> bool:
    """Apply one journal row. Returns True if books may need a recompute."""
    assert st.est is not None
    kind = str(row.get("kind"))
    data = row.get("data")
    ts = float(row.get("ts") or 0.0)
    if not isinstance(data, dict) and kind != "orders_out":
        # orders_out is a list; ignore for market replay
        if kind == "orders_out":
            return False
        return False

    if kind == "book" and isinstance(data, dict):
        upd = parse_book(data)
        if upd is None:
            return False
        book = _book_for(st, upd.asset_id)
        if book is None:
            return False
        if upd.tick_size:
            book.set_tick_size(upd.tick_size)
        book.apply_snapshot(upd.bids, upd.asks, upd.ts or ts, upd.book_hash)
        return True

    if kind == "price_change" and isinstance(data, dict):
        dirty = False
        for ch in parse_price_changes(data):
            book = _book_for(st, ch.asset_id)
            if book is None:
                continue
            book.apply_delta(ch.side, ch.price, ch.size, ch.ts or ts)
            dirty = True
        return dirty

    if kind == "last_trade_price" and isinstance(data, dict):
        tp = parse_last_trade(data)
        if tp is None:
            return False
        if tp.asset_id not in (st.meta.yes.token_id, st.meta.no.token_id):
            return False
        st.est.flow.update(tp.aggressor, tp.size, tp.ts or ts)
        return True

    if kind == "tick_size_change" and isinstance(data, dict):
        tsc = parse_tick_size_change(data)
        if tsc is None:
            return False
        book = _book_for(st, tsc.asset_id)
        if book is not None:
            book.set_tick_size(tsc.tick_size)
        return False

    return False


def run_replay(
    journal_path: Path,
    meta: MarketMeta,
    profile: StrategyProfile,
    metrics_path: Path,
) -> ReplayResult:
    rows = load_journal(journal_path)
    st = ReplayState(meta=meta, profile=profile)
    st.metrics = MetricsLogger(metrics_path, enabled=True)
    st.metrics.emit(
        "market_meta",
        condition_id=meta.condition_id,
        slug=meta.slug,
        rewards_daily_rate=meta.rewards_daily_rate,
        rewards_max_spread=meta.rewards_max_spread,
        rewards_min_size=meta.rewards_min_size,
        rebate_rate=meta.rebate_rate,
        tick_size=meta.tick_size,
    )

    applied = 0
    for row in rows:
        if apply_journal_event(st, row):
            applied += 1
            _recompute(st, float(row.get("ts") or 0.0))

    assert st.metrics is not None
    st.metrics.close()
    return ReplayResult(
        events_read=len(rows),
        events_applied=applied,
        recomputes=st.recomputes,
        metrics_path=str(metrics_path),
        n_quote=st.n_quote,
        n_cancel=st.n_cancel,
        n_mark=st.n_mark,
    )


# silence unused TradePrint/Side import warning via re-export for tests
__all__ = [
    "run_replay",
    "load_journal",
    "ReplayResult",
    "TradePrint",
    "Side",
    "infer_yes_no_tokens",
    "discover_condition_ids",
]
