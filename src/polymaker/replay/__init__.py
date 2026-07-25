"""Deterministic journal replay through the shared decision pipeline.

Reads Journal JSONL (`book` / `price_change` / `last_trade_price`) and drives
OrderBook + estimators + DecisionFramework + RegimeMachine + construct_quotes
+ reconcile + FillSimulator, emitting MetricsLogger events.

Order lifecycle (authoritative):
  CREATED → LIVE → PARTIALLY_FILLED → FILLED
                       └────────→ CANCELLED

After every event that mutates orders:
  replay_live_order_ids == fill_simulator_order_ids
  replay_order_remaining == fill_simulator_remaining
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymaker.accounting.equity_ledger import EquityLedger
from polymaker.config import StrategyProfile
from polymaker.domain import (
    Fill,
    MarketMeta,
    OpenOrder,
    OrderState,
    Position,
    Side,
)
from polymaker.execution.reconciler import reconcile
from polymaker.intelligence import DecisionFramework
from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import (
    TradePrint,
    parse_book,
    parse_last_trade,
    parse_price_changes,
    parse_tick_size_change,
)
from polymaker.metrics import MetricsLogger, inventory_fields
from polymaker.paper.fill_sim import FillSimulator
from polymaker.paper.queue_fill_sim import FillMode, make_fill_simulator
from polymaker.strategy.decision_pipeline import build_targets
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.regime import RegimeMachine


@dataclass
class ReplayResult:
    events_read: int = 0
    events_applied: int = 0
    recomputes: int = 0
    metrics_path: str = ""
    n_quote: int = 0
    n_cancel: int = 0
    n_mark: int = 0
    n_fill: int = 0
    state_divergence_events: int = 0
    fills_after_cancel: int = 0
    overfills: int = 0
    final_equity: float = 0.0
    final_cash: float = 0.0
    fill_mode: str = "conservative"


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
    # Promotion default is conservative; pass fill_mode to run_replay to override.
    fill_sim: Any = field(default=None)
    fill_mode: str = "conservative"
    n_fill: int = 0
    intel: DecisionFramework = field(default_factory=DecisionFramework)
    trade_ts: list[float] = field(default_factory=list)
    ledger: EquityLedger = field(default_factory=EquityLedger)
    # Lifecycle integrity counters
    state_divergence_events: int = 0
    fills_after_cancel: int = 0
    overfills: int = 0
    cancelled_ids: set[str] = field(default_factory=set)
    # Seen trade print ids for dedupe (asset, ts, price, size, side)
    _seen_trades: set[str] = field(default_factory=set)
    strict_sync: bool = True
    # Inventory entry times for exit urgency (token_id -> first fill ts)
    pos_entry_ts: dict[str, float] = field(default_factory=dict)

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
        if self.fill_sim is None:
            self.fill_sim = make_fill_simulator(self.fill_mode)


def assert_order_sync(st: ReplayState) -> list[str]:
    """Return list of divergence messages (empty if in sync)."""
    errs: list[str] = []
    live_ids = set(st.live.keys())
    sim_ids = st.fill_sim.order_ids()
    if live_ids != sim_ids:
        errs.append(
            f"id_set_mismatch live={sorted(live_ids)} sim={sorted(sim_ids)}"
        )
    for oid in live_ids & sim_ids:
        live_rem = st.live[oid].size
        sim_rem = st.fill_sim.remaining(oid)
        if abs(live_rem - sim_rem) > 1e-9:
            errs.append(
                f"remaining_mismatch {oid}: live={live_rem} sim={sim_rem}"
            )
    return errs


def check_and_record_sync(st: ReplayState) -> None:
    errs = assert_order_sync(st)
    if errs:
        st.state_divergence_events += 1
        if st.strict_sync:
            raise AssertionError(
                "replay order state divergence: " + "; ".join(errs)
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
    """Keep journal rows that touch the given YES/NO token ids."""
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
            if any(
                str(ch.get("asset_id") or "") in wanted
                for ch in changes
                if isinstance(ch, dict)
            ):
                out.append(row)
            continue
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


def _sync_live_after_fill(st: ReplayState, fill: Fill) -> None:
    """Update st.live remaining size from fill; remove if fully filled."""
    oid = fill.order_id
    if not oid:
        return
    if oid in st.cancelled_ids:
        st.fills_after_cancel += 1
        return
    o = st.live.get(oid)
    if o is None:
        # Already removed from live but sim filled — divergence unless sim also gone
        if oid in st.fill_sim.order_ids() or st.fill_sim.remaining(oid) > 0:
            st.state_divergence_events += 1
        return
    new_size = o.size - fill.size
    if new_size <= 1e-12:
        st.live.pop(oid, None)
    else:
        st.live[oid] = OpenOrder(
            o.order_id,
            o.token_id,
            o.side,
            o.price,
            new_size,
            OrderState.LIVE,
            o.created_ts,
        )


def _recompute(st: ReplayState, now: float) -> None:
    assert st.est is not None and st.metrics is not None
    meta, p = st.meta, st.profile
    yb, nb = st.yes_book, st.no_book
    if yb.is_empty:
        return
    yes_view = yb.view()
    if yes_view.best_bid is None or yes_view.best_ask is None:
        return
    if yes_view.best_bid >= yes_view.best_ask:
        return

    micro = yb.microprice(p.micro_levels)
    if micro is None:
        return
    # Shared pipeline owns FV + regime + intel + construct_quotes.
    # est.last_fv is still the *previous* FV for jump detection until we update.
    n_trades = len(st.trade_ts)
    last_trade = max(st.trade_ts) if st.trade_ts else 0.0
    secs_stale = (now - last_trade) if last_trade > 0 else 0.0

    # Exit urgency: grow with hold time / exit_urgency_s; toxic markout bumps more.
    def _urgency(token_id: str, size: float) -> float:
        if size <= 0:
            return 0.0
        t0 = st.pos_entry_ts.get(token_id, now)
        hold = max(0.0, now - t0)
        base = min(1.0, hold / max(p.exit_urgency_s, 1.0))
        tox = float(getattr(st.est.markout, "toxicity", 0.0) or 0.0)
        if tox > 0.02:
            base = min(1.0, base + 0.35)
        return base

    result = build_targets(
        meta=meta,
        profile=p,
        yes_view=yes_view,
        no_view=nb.view() if not nb.is_empty else _empty_view(),
        pos_yes=st.pos_yes,
        pos_no=st.pos_no,
        est=st.est,
        regime_machine=st.regime,
        now=now,
        micro=micro,
        intel=st.intel if p.use_intelligence else None,
        n_trades_last_hour=n_trades,
        seconds_since_last_trade=secs_stale,
        yes_exit_urgency=_urgency(meta.yes.token_id, st.pos_yes.size),
        no_exit_urgency=_urgency(meta.no.token_id, st.pos_no.size),
    )
    if result is None:
        return
    st.est.on_fair_value(result.fv, now)

    tq = result.targets
    fv = result.fv
    regime = result.regime
    attr = result.attribution

    live = list(st.live.values())
    plan = reconcile(
        tq,
        live,
        tick=meta.tick_size,
        reprice_ticks=p.reprice_ticks,
        resize_frac=p.resize_frac,
    )
    inv = inventory_fields(st.pos_yes.size, st.pos_no.size)
    st.ledger.update_mark(meta.yes.token_id, fv)
    st.ledger.update_mark(meta.no.token_id, 1.0 - fv)
    st.metrics.emit(
        "mark",
        ts=now,
        condition_id=meta.condition_id,
        fv=fv,
        regime=regime.value,
        intel_decision=attr.intelligence_decision,
        intel_size=attr.size_multiplier,
        intel_band_frac=attr.buy_band_frac if attr.buy_band_frac is not None else -1.0,
        intel_reason=attr.intel_reason[:120] if attr.intel_reason else "",
        equity=st.ledger.equity(),
        **inv,
    )
    st.n_mark += 1
    st.recomputes += 1

    if plan.is_noop:
        check_and_record_sync(st)
        return

    for oid in plan.to_cancel:
        o = st.live.pop(oid, None)
        st.fill_sim.cancel(oid)  # authoritative: cancelled can never fill
        st.cancelled_ids.add(oid)
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
        if oid in st.live or oid in st.fill_sim.order_ids():
            # Uniqueness: recompute index + place index should be unique
            oid = f"replay-{st.recomputes}-{i}-{len(st.live)}"
        o = OpenOrder(oid, q.token_id, q.side, q.price, q.size, created_ts=now)
        st.live[oid] = o
        st.fill_sim.place(o, ts=now)
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
            intel_decision=attr.intelligence_decision,
            buy_offset_ticks=attr.buy_offset_ticks,
            size_multiplier=attr.size_multiplier,
            reason_codes=",".join(attr.reason_codes),
            **inv,
        )
        st.n_quote += 1

    check_and_record_sync(st)


def _apply_replay_fill(st: ReplayState, fill: Fill, *, fv_for_markout: float | None) -> None:
    """Apply a simulated fill to replay positions, ledger, estimators, metrics."""
    assert st.est is not None
    meta = st.meta
    if fill.order_id and fill.order_id in st.cancelled_ids:
        st.fills_after_cancel += 1
        return

    pos = st.pos_yes if fill.token_id == meta.yes.token_id else st.pos_no
    signed = fill.size if fill.side is Side.BUY else -fill.size
    new_size = pos.size + signed
    if fill.side is Side.BUY:
        if pos.size <= 0:
            pos.avg_price = fill.price
        else:
            pos.avg_price = (
                pos.avg_price * pos.size + fill.price * fill.size
            ) / (pos.size + fill.size)
    pos.size = max(0.0, new_size)
    if pos.size <= 0:
        pos.avg_price = 0.0

    st.ledger.apply_fill(fill)
    _sync_live_after_fill(st, fill)
    # Track entry time for exit urgency (first fill that opens inventory)
    if fill.side is Side.BUY and pos.size > 0:
        st.pos_entry_ts.setdefault(fill.token_id, fill.ts)
    if pos.size <= 0:
        st.pos_entry_ts.pop(fill.token_id, None)

    if fv_for_markout is not None:
        token_fv = (
            fv_for_markout
            if fill.token_id == meta.yes.token_id
            else (1.0 - fv_for_markout)
        )
        st.est.markout.record_fill(fill.side, token_fv, fill.ts)
        if getattr(st.profile, "use_intelligence", False):
            tick = max(meta.tick_size, 1e-9)
            offset = int(round((fill.price - token_fv) / tick))
            edge = (
                (token_fv - fill.price)
                if fill.side is Side.BUY
                else (fill.price - token_fv)
            )
            markout = -float(getattr(st.est.markout, "toxicity", 0.0) or 0.0)
            st.intel.record_fill(meta.condition_id, offset, edge, markout)
    else:
        token_fv = fill.price

    inv = inventory_fields(st.pos_yes.size, st.pos_no.size)
    if st.metrics is not None:
        st.metrics.emit(
            "fill",
            ts=fill.ts,
            condition_id=meta.condition_id,
            token_id=fill.token_id,
            side=fill.side.value,
            price=fill.price,
            size=fill.size,
            trade_id=fill.trade_id,
            order_id=fill.order_id,
            mid=token_fv,
            fv=fv_for_markout or fill.price,
            paper=True,
            equity=st.ledger.equity(),
            **inv,
        )
    st.n_fill += 1


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
        # Dedupe identical trade prints
        tkey = (
            f"{tp.asset_id}|{tp.ts or ts}|{tp.price}|{tp.size}|{tp.aggressor.value}"
        )
        if tkey in st._seen_trades:
            return False
        st._seen_trades.add(tkey)

        st.est.flow.update(tp.aggressor, tp.size, tp.ts or ts)
        tts = float(tp.ts or ts)
        st.trade_ts.append(tts)
        cutoff = tts - 3600.0
        st.trade_ts = [t for t in st.trade_ts if t >= cutoff]
        if st.profile.use_intelligence:
            side = "BUY" if tp.aggressor is Side.BUY else "SELL"
            st.intel.update_trade(
                st.meta.condition_id, side, tp.price, tp.size, tts
            )

        # Match fills; never consume more than aggressor size (enforced in sim)
        fills = st.fill_sim.match(
            tp.asset_id, tp.aggressor, tp.price, tp.size, tts
        )
        filled_vol = sum(f.size for f in fills)
        if filled_vol > tp.size + 1e-9:
            st.overfills += 1
        for fill in fills:
            _apply_replay_fill(st, fill, fv_for_markout=st.est.last_fv)
        check_and_record_sync(st)
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
    *,
    strict_sync: bool = True,
    fill_mode: str = "conservative",
) -> ReplayResult:
    """Replay journal. Default fill_mode=conservative for financial claims.

    Use fill_mode='optimistic' only as an upper-bound diagnostic.
    """
    rows = load_journal(journal_path)
    st = ReplayState(meta=meta, profile=profile, fill_mode=fill_mode)
    st.strict_sync = strict_sync
    st.metrics = MetricsLogger(metrics_path, enabled=True)
    first_ts = float(rows[0].get("ts") or 0.0) if rows else 0.0
    st.metrics.emit(
        "market_meta",
        ts=first_ts,
        condition_id=meta.condition_id,
        slug=meta.slug,
        rewards_daily_rate=meta.rewards_daily_rate,
        rewards_max_spread=meta.rewards_max_spread,
        rewards_min_size=meta.rewards_min_size,
        rebate_rate=meta.rebate_rate,
        tick_size=meta.tick_size,
        fill_mode=fill_mode,
    )
    st.ledger.reset_day()

    applied = 0
    for row in rows:
        if apply_journal_event(st, row):
            applied += 1
            _recompute(st, float(row.get("ts") or 0.0))

    # Final sync check
    check_and_record_sync(st)
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
        n_fill=st.n_fill,
        state_divergence_events=st.state_divergence_events,
        fills_after_cancel=st.fills_after_cancel,
        overfills=st.overfills,
        final_equity=st.ledger.equity(),
        final_cash=st.ledger.cash,
        fill_mode=fill_mode,
    )


__all__ = [
    "run_replay",
    "load_journal",
    "ReplayResult",
    "ReplayState",
    "TradePrint",
    "Side",
    "infer_yes_no_tokens",
    "discover_condition_ids",
    "assert_order_sync",
    "apply_journal_event",
    "filter_rows_for_tokens",
]
