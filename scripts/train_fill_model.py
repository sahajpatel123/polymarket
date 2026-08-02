#!/usr/bin/env python3
"""Train and persist the fill model from raw Polymaker journal files.

The raw journal contains ``book`` snapshots, ``price_change`` touch updates
and ``last_trade_price`` events, not strategy fills. This trainer
reconstructs the scalp-hot experiment:

* place synthetic BUY/SELL candidates at each observed touch;
* label a candidate filled when the next touch/trade crosses that price;
* label non-crossed candidates as non-fills;
* compute a 30-second signed markout from subsequent book mids;
* train and persist the model plus the complete bounded training buffer.

``price_change`` events (Polymarket's dense book-delta feed) carry per-asset
``best_bid``/``best_ask``; they are the primary fill source on wide-spread
journals where trade prints never cross the touch.

Unlike a fixed-feature trainer, the book/market features are recomputed from
the tape with real variance — vol_ratio, flow_z and toxicity are rolling
estimates over the observed mids/trades, hours_to_resolve winds down to the
end of the journal, and the regime is derived from the same signals. The
offline model therefore generalizes across book shapes instead of memorizing
one constant regime.

Reproduction (the shipped artifact came from the 24h backtest journal):

    uv run python scripts/train_fill_model.py \\
        --journal backtest_24h/journal.jsonl --output models/fill_model.pkl

Real live journal (dense price_change feed):

    uv run python scripts/train_fill_model.py \\
        --journal journal/paper.jsonl --output models/fill_model.pkl

This is deliberately an offline trainer. The engine loads the resulting local
artifact before it can quote, and continues adding live samples afterwards.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from polymaker.domain import Side
from polymaker.strategy.fill_model import FillFeatures, FillModel, FillTrainingStore


@dataclass(frozen=True, slots=True)
class BookState:
    asset_id: str
    ts: float
    best_bid: float
    best_ask: float
    bid_depth: float
    ask_depth: float
    bid_level1: float
    ask_level1: float
    ask_level2: float
    bid_level2: float
    mid: float
    micro: float
    imbalance: float
    spread_ticks: float


@dataclass(frozen=True, slots=True)
class Candidate:
    features: FillFeatures
    asset_id: str
    side: Side
    ts: float
    mid: float
    micro: float
    price: float


class _Tape:
    """Per-asset rolling tape state → live-like feature estimates.

    These are deliberate proxies for the engine estimators (VolEstimator,
    FlowEstimator, markout toxicity): recent vs longer mid volatility,
    z-score of recent signed trade flow, and interval toxicity (did the mid
    move in the direction of the flow since the last snapshot).
    """

    def __init__(self) -> None:
        self.mids: list[tuple[float, float]] = []  # (ts, mid)
        self.signed: list[float] = []  # per-trade signed size
        self.trade_ts: list[float] = []
        self.interval_tox: list[float] = []  # toxicity per book interval
        self._pending_flow = 0.0

    def on_book(self, ts: float, mid: float) -> None:
        if self.mids:
            move = mid - self.mids[-1][1]
            if self._pending_flow != 0.0:
                # Positive: price moved in the direction of the flow
                # (informed flow pushed it) = toxic for a resting quote.
                tox = move * math.copysign(1.0, self._pending_flow)
                self.interval_tox.append(tox)
                if len(self.interval_tox) > 20:
                    self.interval_tox = self.interval_tox[-20:]
            self._pending_flow = 0.0
        self.mids.append((ts, mid))
        if len(self.mids) > 50:
            self.mids = self.mids[-50:]

    def on_trade(self, signed_size: float, ts: float) -> None:
        self._pending_flow += signed_size
        self.signed.append(signed_size)
        self.trade_ts.append(ts)
        if len(self.signed) > 200:
            self.signed = self.signed[-200:]
            self.trade_ts = self.trade_ts[-200:]

    def vol_ratio(self) -> float:
        if len(self.mids) < 13:
            return 1.0
        diffs = np.diff([m for _, m in self.mids[-13:]])
        short = float(np.std(diffs[-3:])) if len(diffs) >= 3 else 1.0
        long = float(np.std(diffs))
        if long < 1e-12:
            return 1.0
        return _clip(short / max(long, 1e-12), 0.0, 20.0)

    def flow_z(self) -> float:
        if len(self.signed) < 20:
            return 0.0
        recent = self.signed[-20:]
        if len(self.signed) >= 100:
            mu = float(np.mean(self.signed[-100:]))
            sd = float(np.std(self.signed[-100:]))
        else:
            mu = 0.0
            sd = float(np.std(self.signed))
        if sd < 1e-12:
            return 0.0
        z = (sum(recent) - 20.0 * mu) / (sd * math.sqrt(20.0))
        return _clip(z, -10.0, 10.0)

    def toxicity(self) -> float:
        if not self.interval_tox:
            return 0.0
        return _clip(float(np.mean(self.interval_tox[-10:])), 0.0, 2.0)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _derive_regime(flow_z: float, toxicity: float, vol_ratio: float) -> str:
    if toxicity > 0.05 or abs(flow_z) > 3.0:
        return "EVENT"
    if abs(flow_z) > 1.5 or vol_ratio > 2.0:
        return "TRENDING"
    return "QUIET"


def _book_state(asset_id: str, ts: float, data: dict[str, Any], tick: float) -> BookState | None:
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        return None
    try:
        bid_rows = [{"price": float(x["price"]), "size": float(x["size"])} for x in bids[:10]]
        ask_rows = [{"price": float(x["price"]), "size": float(x["size"])} for x in asks[:10]]
    except (KeyError, TypeError, ValueError):
        return None
    bb = bid_rows[0]["price"]
    ba = ask_rows[0]["price"]
    bd = sum(x["size"] for x in bid_rows)
    ad = sum(x["size"] for x in ask_rows)
    mid = (bb + ba) / 2.0
    b3 = sum(x["size"] for x in bid_rows[:3])
    a3 = sum(x["size"] for x in ask_rows[:3])
    micro = (ba * b3 + bb * a3) / (b3 + a3) if b3 + a3 > 0.0 else mid
    return BookState(
        asset_id=asset_id,
        ts=ts,
        best_bid=bb,
        best_ask=ba,
        bid_depth=bd,
        ask_depth=ad,
        bid_level1=bid_rows[0]["size"],
        ask_level1=ask_rows[0]["size"],
        ask_level2=sum(x["size"] for x in ask_rows[1:4]),
        bid_level2=sum(x["size"] for x in bid_rows[1:4]),
        mid=mid,
        micro=micro,
        imbalance=(bd - ad) / max(bd + ad, 1e-9),
        spread_ticks=(ba - bb) / max(tick, 1e-9),
    )


def _features(
    book: BookState,
    side: Side,
    *,
    base_size_usdc: float,
    tick: float,
    hours: float,
    vol_ratio: float,
    flow_z: float,
    toxicity: float,
    regime: str,
    ofi: float,
    ofi_trend: float,
    size_anomaly: float,
    trade_rate: float,
    price: float | None = None,
    depth: float | None = None,
    at_touch: float = 1.0,
) -> FillFeatures:
    price = book.best_bid if price is None else price
    depth = book.bid_depth if depth is None else depth
    return FillFeatures(
        book_imbalance=max(-1.0, min(1.0, book.imbalance)),
        spread_ticks=max(0.0, min(200.0, book.spread_ticks)),
        at_touch=at_touch,
        # These are deliberately pre-fill values. No future markout is used
        # as a feature, preventing target leakage.
        vol_ratio=vol_ratio,
        flow_z=flow_z,
        toxicity=toxicity,
        mid_price=max(0.01, min(0.99, book.mid)),
        our_size_vs_depth=max(0.0, min(5.0, (base_size_usdc / max(price, tick)) / max(depth, 1e-9))),
        hours_to_resolve=min(hours, 720.0),
        quote_dist_from_mid_ticks=abs(price - book.mid) / max(tick, 1e-9),
        regime_quiet=1.0 if regime == "QUIET" else 0.0,
        regime_trending=1.0 if regime == "TRENDING" else 0.0,
        regime_event=1.0 if regime == "EVENT" else 0.0,
        regime_reduce_only=0.0,
        regime_halted=0.0,
        ofi=ofi,
        ofi_trend=ofi_trend,
        size_anomaly=size_anomaly,
        trade_rate=trade_rate,
        bd_total=book.bid_depth,
        ad_total=book.ask_depth,
        ad1=book.ask_level1,
        ad2=book.ask_level2,
        bd1=book.bid_level1,
        bd2=book.bid_level2,
    )


def _matches(candidate: Candidate, price: float, aggressor: str, tick: float) -> bool:
    if candidate.side is Side.BUY:
        return aggressor == "SELL" and price <= candidate.price + 2.0 * tick
    return aggressor == "BUY" and price >= candidate.price - 2.0 * tick


def build_training_store(
    journals: list[Path], *, tick: float = 0.001, base_size_usdc: float = 4.0,
    max_events: int = 5_000_000, offsets_ticks: tuple[int, ...] = (0, 1, 2),
    max_samples: int = 500_000,
) -> tuple[FillTrainingStore, dict[str, int]]:
    """Convert raw journal events into aligned fill/non-fill samples.

    Consumes ``book`` (full L2), ``price_change`` (dense level deltas with
    best_bid/best_ask) and ``last_trade_price`` (prints). A synthetic live
    book per asset is maintained from the delta stream (matching the engine's
    OrderBook) so depth features are fresh at every candidate. Candidates are
    placed at each touch-state change at ``offsets_ticks`` behind the touch
    (offset 0 = at-touch) to mirror the strategy's layered quote placement; a
    candidate fills when a trade print crosses its price (2-tick tolerance)
    or, at the touch, the book strictly inverts through it. So journals with
    a dense price_change feed but sparse prints still reconstruct fills.
    """
    events: list[tuple[float, int, dict[str, Any]]] = []
    sequence = 0
    for path in journals:
        with path.open() as fh:
            for line in fh:
                if len(events) >= max_events:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("kind") or event.get("event")
                if kind not in ("book", "last_trade_price", "price_change"):
                    continue
                events.append((float(event.get("ts", 0.0)), sequence, event))
                sequence += 1
    events.sort(key=lambda x: (x[0], x[1]))

    # Per-asset journal end (for a winding-down hours_to_resolve feature).
    asset_end: dict[str, float] = {}
    for ts, _seq, event in events:
        data = event.get("data") or {}
        kind = event.get("kind") or event.get("event")
        if kind == "price_change":
            for pc in data.get("price_changes") or []:
                asset = str(pc.get("asset_id") or "")
                if asset:
                    asset_end[asset] = max(asset_end.get(asset, 0.0), ts)
            continue
        asset = str(data.get("asset_id") or data.get("token_id") or "")
        if asset:
            asset_end[asset] = max(asset_end.get(asset, 0.0), ts)

    books_by_asset: dict[str, list[tuple[float, float]]] = {}
    # synthetic L2 books, maintained live from price_change level deltas so
    # depth features are never stale (full `book` snapshots are rare).
    synth: dict[str, tuple[dict[float, float], dict[float, float]]] = {}
    current: dict[str, BookState] = {}
    last_touch: dict[str, tuple[float, float]] = {}
    last_l1: dict[str, tuple[float, float]] = {}
    ofi_history: dict[str, list[float]] = {}
    pending: dict[str, dict[Side, list[Candidate]]] = {}
    tapes: dict[str, _Tape] = {}
    filled: list[tuple[Candidate, float]] = []
    nonfills: list[FillFeatures] = []

    def book_state(asset: str, ts: float, bb: float, ba: float,
                   bids: dict[float, float], asks: dict[float, float]) -> BookState | None:
        """Fresh BookState from the synthetic book + reported touch."""
        if not bids or not asks:
            return None
        b_rows = sorted(bids.items(), key=lambda x: -x[0])[:10]
        a_rows = sorted(asks.items(), key=lambda x: x[0])[:10]
        bd = sum(sz for _, sz in b_rows)
        ad = sum(sz for _, sz in a_rows)
        mid = (bb + ba) / 2.0
        # Depth-weighted microprice, same definition as the engine's
        # OrderBook.microprice (top-3 levels) — the dominant FV component,
        # so offline markout labels align with the engine's FV labels.
        b3 = sum(sz for _, sz in b_rows[:3])
        a3 = sum(sz for _, sz in a_rows[:3])
        if b3 + a3 > 0.0:
            micro = (ba * b3 + bb * a3) / (b3 + a3)
        else:
            micro = mid
        return BookState(
            asset_id=asset, ts=ts, best_bid=bb, best_ask=ba,
            bid_depth=bd, ask_depth=ad,
            bid_level1=b_rows[0][1] if b_rows else 0.0,
            ask_level1=a_rows[0][1] if a_rows else 0.0,
            ask_level2=sum(sz for _, sz in a_rows[1:4]),
            bid_level2=sum(sz for _, sz in b_rows[1:4]),
            mid=mid, micro=micro,
            imbalance=(bd - ad) / max(bd + ad, 1e-9),
            spread_ticks=(ba - bb) / max(tick, 1e-9),
        )

    def finalize(asset: str) -> None:
        old = pending.pop(asset, None)
        if old:
            for candidates in old.values():
                nonfills.extend(c.features for c in candidates)

    def place_candidates(asset: str, state: BookState, ts: float) -> None:
        """Refresh resting candidates at the new touch (one per offset)."""
        finalize(asset)
        tape = tapes.setdefault(asset, _Tape())
        fz = tape.flow_z()
        tox = tape.toxicity()
        vr = tape.vol_ratio()
        regime = _derive_regime(fz, tox, vr)
        hours = max(0.0, (asset_end.get(asset, ts) - ts) / 3600.0)
        recent_sizes = [abs(x) for x in tape.signed[-20:]]
        avg_size = sum(recent_sizes) / len(recent_sizes) if recent_sizes else 1.0
        size_anomaly = recent_sizes[-1] / max(avg_size, 1.0) if recent_sizes else 1.0
        trade_rate = len([x for x in tape.trade_ts if ts - x <= 30.0]) / 30.0
        ofi_hist = ofi_history.setdefault(asset, [])
        normalized_ofi = ofi_hist[-1] if ofi_hist else 0.0
        ofi_trend = normalized_ofi - ofi_hist[0] if len(ofi_hist) >= 3 else 0.0
        buys: list[Candidate] = []
        sells: list[Candidate] = []
        for k in offsets_ticks:
            buy_price = state.best_bid - k * tick
            sell_price = state.best_ask + k * tick
            feats_buy = _features(
                state, Side.BUY, base_size_usdc=base_size_usdc, tick=tick,
                hours=hours, vol_ratio=vr, flow_z=fz, toxicity=tox, regime=regime,
                ofi=normalized_ofi, ofi_trend=ofi_trend,
                size_anomaly=size_anomaly, trade_rate=trade_rate,
                price=buy_price, depth=state.bid_depth, at_touch=1.0 if k == 0 else 0.0,
            )
            feats_sell = _features(
                state, Side.SELL, base_size_usdc=base_size_usdc, tick=tick,
                hours=hours, vol_ratio=vr, flow_z=fz, toxicity=tox, regime=regime,
                ofi=-normalized_ofi, ofi_trend=-ofi_trend,
                size_anomaly=size_anomaly, trade_rate=trade_rate,
                price=sell_price, depth=state.ask_depth, at_touch=1.0 if k == 0 else 0.0,
            )
            buys.append(Candidate(feats_buy, asset, Side.BUY, ts, state.mid, state.micro, buy_price))
            sells.append(Candidate(feats_sell, asset, Side.SELL, ts, state.mid, state.micro, sell_price))
        pending[asset] = {Side.BUY: buys, Side.SELL: sells}

    def on_touch_move(asset: str, bb: float, ba: float, ts: float) -> None:
        """Touch moved: rebuild state from the synthetic book, refresh quotes."""
        bids, asks = synth.get(asset, ({}, {}))
        state = book_state(asset, ts, bb, ba, bids, asks)
        if state is None:
            return
        current[asset] = state
        books_by_asset.setdefault(asset, []).append((ts, state.mid, state.micro))
        tapes.setdefault(asset, _Tape()).on_book(ts, state.mid)
        # OFI from level-1 size changes between placements.
        ofi_hist = ofi_history.setdefault(asset, [])
        l1 = last_l1.get(asset)
        if l1 is not None:
            raw_ofi = (state.bid_level1 - l1[0]) - (state.ask_level1 - l1[1])
            normalized = raw_ofi / max(state.bid_level1 + state.ask_level1, 1.0)
            ofi_hist.append(normalized)
            if len(ofi_hist) > 20:
                del ofi_hist[:-20]
        last_l1[asset] = (state.bid_level1, state.ask_level1)
        place_candidates(asset, state, ts)

    def check_touch_fill(asset: str, bb: float, ba: float, ts: float) -> None:
        """Touch crossed through an AT-TOUCH resting candidate (strict: only
        a real trade-through, i.e. the book inverted at our level, counts)."""
        candidates = pending.get(asset, {})
        buys = candidates.get(Side.BUY, [])
        if buys and ba <= buys[0].price:
            filled.append((buys[0], ts))
            del buys[0]
        sells = candidates.get(Side.SELL, [])
        if sells and bb >= sells[0].price:
            filled.append((sells[0], ts))
            del sells[0]

    def check_print_fill(asset: str, price: float, aggressor: str, ts: float) -> None:
        """A trade print crossed a resting candidate: label it filled."""
        candidates = pending.get(asset, {})
        for side in (Side.BUY, Side.SELL):
            rest = candidates.get(side, [])
            keep: list[Candidate] = []
            for candidate in rest:
                if _matches(candidate, price, aggressor, tick):
                    filled.append((candidate, ts))
                else:
                    keep.append(candidate)
            candidates[side] = keep

    for ts, _seq, event in events:
        data = event.get("data") or {}
        kind = event.get("kind") or event.get("event")

        if kind == "price_change":
            for pc in data.get("price_changes") or []:
                asset = str(pc.get("asset_id") or "")
                if not asset:
                    continue
                try:
                    bb = float(pc["best_bid"])
                    ba = float(pc["best_ask"])
                    px = float(pc["price"])
                    sz = float(pc.get("size", 0.0) or 0.0)
                    side = str(pc.get("side", "")).upper()
                except (KeyError, TypeError, ValueError):
                    continue
                bids, asks = synth.get(asset, ({}, {}))
                if side == "BUY":
                    if sz <= 0.0:
                        bids.pop(px, None)
                    else:
                        bids[px] = sz
                elif side == "SELL":
                    if sz <= 0.0:
                        asks.pop(px, None)
                    else:
                        asks[px] = sz
                synth[asset] = (bids, asks)
                touch = last_touch.get(asset)
                if touch is not None and abs(touch[0] - bb) < 1e-12 and abs(touch[1] - ba) < 1e-12:
                    continue  # touch unchanged -> levels only; keep candidates resting
                last_touch[asset] = (bb, ba)
                check_touch_fill(asset, bb, ba, ts)
                on_touch_move(asset, bb, ba, ts)
            continue

        asset = str(data.get("asset_id") or data.get("token_id") or "")
        if not asset:
            continue

        if kind == "book":
            state = _book_state(asset, ts, data, tick)
            if state is None:
                continue
            bids = {float(r["price"]): float(r["size"]) for r in (data.get("bids") or []) if float(r["size"]) > 0.0}
            asks = {float(r["price"]): float(r["size"]) for r in (data.get("asks") or []) if float(r["size"]) > 0.0}
            synth[asset] = (bids, asks)
            current[asset] = state
            books_by_asset.setdefault(asset, []).append((ts, state.mid, state.micro))
            tape = tapes.setdefault(asset, _Tape())
            tape.on_book(ts, state.mid)
            last_touch[asset] = (state.best_bid, state.best_ask)
            last_l1[asset] = (state.bid_level1, state.ask_level1)
            ofi_history.pop(asset, None)
            check_touch_fill(asset, state.best_bid, state.best_ask, ts)
            place_candidates(asset, state, ts)
            continue

        state = current.get(asset)
        if state is None:
            continue
        try:
            price = float(data["price"])
            size = float(data.get("size", 0.0) or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        aggressor = str(data.get("side", "")).upper()
        if aggressor == "BUY":
            tapes.setdefault(asset, _Tape()).on_trade(size, ts)
        elif aggressor == "SELL":
            tapes.setdefault(asset, _Tape()).on_trade(-size, ts)
        check_print_fill(asset, price, aggressor, ts)

    for asset in list(pending):
        finalize(asset)

    training = FillTrainingStore(max_samples=max_samples)
    fill_meta: list[dict[str, str]] = []
    markout_count = 0
    for candidate, fill_ts in filled:
        series = books_by_asset.get(candidate.asset_id, [])
        times = [x[0] for x in series]
        idx = bisect.bisect_left(times, fill_ts + 30.0)
        if idx >= len(series):
            continue
        _ts, _mid, future_micro = series[idx]
        move = future_micro - candidate.micro
        markout = move if candidate.side is Side.BUY else -move
        training.add(candidate.features, filled=True, markout=markout,
                     source="offline")
        fill_meta.append({"asset": candidate.asset_id,
                          "side": candidate.side.value,
                          "offset_ticks": str(int(round((candidate.mid - candidate.price) / tick)))
                          if candidate.side is Side.BUY
                          else str(int(round((candidate.price - candidate.mid) / tick)))})
        markout_count += 1

    for features in nonfills:
        training.add(features, filled=False, markout=0.0, source="offline")

    stats = {
        "events": len(events),
        "filled_candidates": len(filled),
        "filled_with_markout": markout_count,
        "nonfill_candidates": len(nonfills),
        "samples": len(training.features),
    }
    if fill_meta:
        stats["fill_meta"] = fill_meta
    return training, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", action="append", required=True, help="raw journal JSONL; repeatable")
    parser.add_argument("--output", default="models/fill_model.pkl")
    parser.add_argument("--tick-size", type=float, default=0.001)
    parser.add_argument("--base-size-usdc", type=float, default=4.0)
    parser.add_argument("--max-events", type=int, default=5_000_000)
    parser.add_argument("--offsets-ticks", type=str, default="0,1,2",
                        help="comma-separated quote offsets behind the touch")
    args = parser.parse_args()

    paths = [Path(x) for x in args.journal]
    missing = [str(x) for x in paths if not x.exists()]
    if missing:
        parser.error(f"journal not found: {', '.join(missing)}")
    offsets = tuple(int(x) for x in args.offsets_ticks.split(",") if x.strip() != "")

    store, stats = build_training_store(
        paths, tick=args.tick_size, base_size_usdc=args.base_size_usdc,
        max_events=args.max_events, offsets_ticks=offsets,
    )
    if stats["filled_candidates"] == 0:
        raise SystemExit(
            "no fills reconstructed — the books and trades in these journals "
            f"do not cross the touch ({stats}). Use a journal where book and "
            "last_trade_price events align (e.g. backtest_24h/journal.jsonl)."
        )
    arrays = store.to_arrays()
    if arrays is None:
        raise SystemExit(f"insufficient training samples: {stats}")
    X, y_fill, y_markout = arrays
    model = FillModel(min_samples=100)
    model.train(X, y_fill, y_markout)
    if not model.is_trained:
        raise SystemExit(f"model did not reach trained state: {stats}")
    model.save(args.output, store)
    print(json.dumps({**stats, "trained": model.is_trained, "output": args.output}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
