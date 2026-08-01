#!/usr/bin/env python3
"""Train and persist the fill model from raw Polymaker journal files.

The raw journal contains ``book`` snapshots and ``last_trade_price`` events,
not strategy fills. This trainer reconstructs the scalp-hot experiment:

* place synthetic BUY/SELL candidates at each observed touch;
* label a candidate filled when the next trade crosses that touch;
* label non-crossed candidates as non-fills;
* compute a 30-second signed markout from subsequent book mids;
* train and persist the model plus the complete bounded training buffer.

Unlike a fixed-feature trainer, the book/market features are recomputed from
the tape with real variance — vol_ratio, flow_z and toxicity are rolling
estimates over the observed mids/trades, hours_to_resolve winds down to the
end of the journal, and the regime is derived from the same signals. The
offline model therefore generalizes across book shapes instead of memorizing
one constant regime.

Reproduction (the shipped artifact came from the 24h backtest journal):

    uv run python scripts/train_fill_model.py \\
        --journal backtest_24h/journal.jsonl --output models/fill_model.pkl

Note: ``journal/paper.jsonl`` (wide-spread, at-resolution books whose trade
prints do not cross the touch) reconstructs zero fills and exits non-zero —
use a journal where books and trades align.

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
    imbalance: float
    spread_ticks: float


@dataclass(frozen=True, slots=True)
class Candidate:
    features: FillFeatures
    asset_id: str
    side: Side
    ts: float
    mid: float
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

    def on_trade(self, signed_size: float) -> None:
        self._pending_flow += signed_size
        self.signed.append(signed_size)
        if len(self.signed) > 200:
            self.signed = self.signed[-200:]

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
        mid=(bb + ba) / 2.0,
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
) -> FillFeatures:
    price = book.best_bid if side is Side.BUY else book.best_ask
    depth = book.bid_depth if side is Side.BUY else book.ask_depth
    return FillFeatures(
        book_imbalance=max(-1.0, min(1.0, book.imbalance)),
        spread_ticks=max(0.0, min(200.0, book.spread_ticks)),
        at_touch=1.0,
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
    )


def _matches(candidate: Candidate, price: float, aggressor: str, tick: float) -> bool:
    if candidate.side is Side.BUY:
        return aggressor == "SELL" and price <= candidate.price + 2.0 * tick
    return aggressor == "BUY" and price >= candidate.price - 2.0 * tick


def build_training_store(
    journals: list[Path], *, tick: float = 0.001, base_size_usdc: float = 4.0,
    max_events: int = 5_000_000,
) -> tuple[FillTrainingStore, dict[str, int]]:
    """Convert raw journal events into aligned fill/non-fill samples."""
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
                if kind not in ("book", "last_trade_price"):
                    continue
                events.append((float(event.get("ts", 0.0)), sequence, event))
                sequence += 1
    events.sort(key=lambda x: (x[0], x[1]))

    # Per-asset journal end (for a winding-down hours_to_resolve feature).
    asset_end: dict[str, float] = {}
    for ts, _seq, event in events:
        data = event.get("data") or {}
        asset = str(data.get("asset_id") or data.get("token_id") or "")
        if asset:
            asset_end[asset] = max(asset_end.get(asset, 0.0), ts)

    books_by_asset: dict[str, list[tuple[float, float]]] = {}
    current: dict[str, BookState] = {}
    pending: dict[str, dict[Side, Candidate]] = {}
    tapes: dict[str, _Tape] = {}
    filled: list[tuple[Candidate, float]] = []
    nonfills: list[FillFeatures] = []

    def finalize(asset: str) -> None:
        old = pending.pop(asset, None)
        if old:
            nonfills.extend(candidate.features for candidate in old.values())

    for ts, _seq, event in events:
        data = event.get("data") or {}
        kind = event.get("kind") or event.get("event")
        asset = str(data.get("asset_id") or data.get("token_id") or "")
        if not asset:
            continue

        if kind == "book":
            finalize(asset)
            state = _book_state(asset, ts, data, tick)
            if state is None:
                continue
            current[asset] = state
            books_by_asset.setdefault(asset, []).append((ts, state.mid))
            tape = tapes.setdefault(asset, _Tape())
            tape.on_book(ts, state.mid)
            fz = tape.flow_z()
            tox = tape.toxicity()
            vr = tape.vol_ratio()
            regime = _derive_regime(fz, tox, vr)
            hours = max(0.0, (asset_end.get(asset, ts) - ts) / 3600.0)
            feats_buy = _features(
                state, Side.BUY, base_size_usdc=base_size_usdc, tick=tick,
                hours=hours, vol_ratio=vr, flow_z=fz, toxicity=tox, regime=regime,
            )
            feats_sell = _features(
                state, Side.SELL, base_size_usdc=base_size_usdc, tick=tick,
                hours=hours, vol_ratio=vr, flow_z=fz, toxicity=tox, regime=regime,
            )
            pending[asset] = {
                Side.BUY: Candidate(
                    feats_buy, asset, Side.BUY, ts, state.mid, state.best_bid,
                ),
                Side.SELL: Candidate(
                    feats_sell, asset, Side.SELL, ts, state.mid, state.best_ask,
                ),
            }
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
            tapes.setdefault(asset, _Tape()).on_trade(size)
        elif aggressor == "SELL":
            tapes.setdefault(asset, _Tape()).on_trade(-size)
        candidates = pending.get(asset, {})
        for side, candidate in list(candidates.items()):
            if _matches(candidate, price, aggressor, tick):
                filled.append((candidate, ts))
                candidates.pop(side, None)

    for asset in list(pending):
        finalize(asset)

    training = FillTrainingStore()
    markout_count = 0
    for candidate, fill_ts in filled:
        series = books_by_asset.get(candidate.asset_id, [])
        times = [x[0] for x in series]
        idx = bisect.bisect_left(times, fill_ts + 30.0)
        if idx >= len(series):
            continue
        future_mid = series[idx][1]
        move = future_mid - candidate.mid
        markout = move if candidate.side is Side.BUY else -move
        training.add(candidate.features, filled=True, markout=markout,
                     source="offline")
        markout_count += 1

    for features in nonfills:
        training.add(features, filled=False, markout=0.0, source="offline")

    return training, {
        "events": len(events),
        "filled_candidates": len(filled),
        "filled_with_markout": markout_count,
        "nonfill_candidates": len(nonfills),
        "samples": len(training.features),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", action="append", required=True, help="raw journal JSONL; repeatable")
    parser.add_argument("--output", default="models/fill_model.pkl")
    parser.add_argument("--tick-size", type=float, default=0.001)
    parser.add_argument("--base-size-usdc", type=float, default=4.0)
    parser.add_argument("--max-events", type=int, default=5_000_000)
    args = parser.parse_args()

    paths = [Path(x) for x in args.journal]
    missing = [str(x) for x in paths if not x.exists()]
    if missing:
        parser.error(f"journal not found: {', '.join(missing)}")

    store, stats = build_training_store(
        paths, tick=args.tick_size, base_size_usdc=args.base_size_usdc,
        max_events=args.max_events,
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
