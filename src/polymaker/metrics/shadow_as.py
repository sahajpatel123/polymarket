"""Shadow adverse-selection from quote lifetimes (no fills required).

Paper mode fabricates orders but does not simulate fills, so classic fill
markouts stay empty. This module measures how mid/FV moved *while a quote
was resting* — a fill-independent adverse-selection / pickoff proxy for
Tier-2 evidence while waiting on fills.

Positive signed mid-move after a BUY is good (market moved our way); negative
is adverse. "Crossed" counts how often mid traded through the resting price
before the quote was cancelled or replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymaker.metrics.analyze import MARKOUT_HORIZONS_S, load_events


@dataclass
class _Resting:
    order_id: str
    condition_id: str
    token_id: str
    side: str  # YES-space side for markout/cross
    book_side: str  # original quote side (active-key)
    price: float
    mid0: float
    ts0: float


@dataclass
class ShadowASReport:
    path: str
    n_quote_lifetimes: int = 0
    n_crossed: int = 0
    crossed_frac: float = 0.0
    mean_edge_at_place: float = 0.0
    mean_lifetime_s: float = 0.0
    markout_mean: dict[str, float] = field(default_factory=dict)
    markout_n: dict[str, int] = field(default_factory=dict)
    by_market: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_regime_at_place: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "n_quote_lifetimes": self.n_quote_lifetimes,
            "n_crossed": self.n_crossed,
            "crossed_frac": round(self.crossed_frac, 6),
            "mean_edge_at_place": round(self.mean_edge_at_place, 6),
            "mean_lifetime_s": round(self.mean_lifetime_s, 6),
            "markout_mean": {k: round(v, 6) for k, v in self.markout_mean.items()},
            "markout_n": dict(self.markout_n),
            "by_market": self.by_market,
            "by_regime_at_place": self.by_regime_at_place,
        }


def _edge(side: str, price: float, mid: float) -> float:
    if side == "BUY":
        return mid - price
    if side == "SELL":
        return price - mid
    return 0.0


def _signed_move(side: str, mid0: float, mid1: float) -> float:
    move = mid1 - mid0
    return move if side == "BUY" else -move


def _crossed(side: str, price: float, mid: float) -> bool:
    if side == "BUY":
        return mid <= price
    if side == "SELL":
        return mid >= price
    return False


def _close_bucket() -> dict[str, Any]:
    return {
        "n": 0,
        "n_crossed": 0,
        "edge_sum": 0.0,
        "life_sum": 0.0,
        "markout_sums": {f"{int(h)}s": 0.0 for h in MARKOUT_HORIZONS_S},
        "markout_ns": {f"{int(h)}s": 0 for h in MARKOUT_HORIZONS_S},
    }


def analyze_shadow_as(path: Path) -> ShadowASReport:
    events, _, _ = load_events(path)
    rep = ShadowASReport(path=str(path))

    marks_by_cid: dict[str, list[tuple[float, float]]] = {}
    regime_at_mark: dict[str, list[tuple[float, str]]] = {}
    for e in events:
        if e.get("event") != "mark":
            continue
        cid = str(e.get("condition_id") or "")
        ts = float(e.get("ts") or 0.0)
        fv = e.get("fv")
        if cid and fv is not None:
            marks_by_cid.setdefault(cid, []).append((ts, float(fv)))
        regime = e.get("regime")
        if cid and regime is not None:
            regime_at_mark.setdefault(cid, []).append((ts, str(regime)))

    def nearest_yes_fv(cid: str, ts: float) -> float | None:
        hist = marks_by_cid.get(cid) or []
        prev: float | None = None
        for mts, mfv in hist:
            if mts <= ts:
                prev = mfv
            else:
                return prev if prev is not None else mfv
        return prev

    def to_yes_space(
        *,
        mid: float,
        price: float,
        side: str,
        cid: str,
        ts: float,
        fv_yes: float | None = None,
    ) -> tuple[float, float, str] | None:
        """Map token-local quote fields onto YES FV space used by mark events.

        Engine logs quote.mid in token space (YES mid or NO mid) but mark.fv
        is always YES fair value. Prefer quote.fv_yes when present (T1-35);
        else fall back to nearest mark FV.
        """
        fv = fv_yes if fv_yes is not None else nearest_yes_fv(cid, ts)
        if fv is None:
            return None
        as_yes = abs(mid - fv) <= abs((1.0 - mid) - fv)
        if as_yes:
            return mid, price, side
        yes_mid = 1.0 - mid
        yes_price = 1.0 - price
        # BUY NO ≡ SELL YES for signed mid moves
        yes_side = "SELL" if side == "BUY" else "BUY"
        return yes_mid, yes_price, yes_side

    # Active restings keyed by (cid, token, side) — replace closes prior life.
    active: dict[tuple[str, str, str], _Resting] = {}
    by_oid: dict[str, _Resting] = {}
    closed: list[tuple[_Resting, float]] = []  # resting, ts_end

    def close(r: _Resting, ts_end: float) -> None:
        closed.append((r, ts_end))
        key = (r.condition_id, r.token_id, r.book_side)
        cur = active.get(key)
        if cur is not None and cur.order_id == r.order_id:
            active.pop(key, None)
        by_oid.pop(r.order_id, None)

    for e in events:
        ev = str(e.get("event"))
        ts = float(e.get("ts") or 0.0)
        cid = str(e.get("condition_id") or "")
        if ev == "quote":
            oid = str(e.get("order_id") or "")
            token = str(e.get("token_id") or "")
            side = str(e.get("side") or "")
            price = float(e.get("price") or 0.0)
            mid = e.get("mid")
            if mid is None:
                continue
            mid0 = float(mid)
            if not oid or not cid or side not in ("BUY", "SELL"):
                continue
            fv_yes_raw = e.get("fv_yes")
            fv_yes = float(fv_yes_raw) if fv_yes_raw is not None else None
            mapped = to_yes_space(
                mid=mid0, price=price, side=side, cid=cid, ts=ts, fv_yes=fv_yes
            )
            if mapped is None:
                continue
            yes_mid, yes_price, yes_side = mapped
            key = (cid, token, side)
            prev = active.get(key)
            if prev is not None and prev.order_id != oid:
                close(prev, ts)
            resting = _Resting(
                order_id=oid,
                condition_id=cid,
                token_id=token,
                side=yes_side,  # YES-space side for markout/cross
                book_side=side,
                price=yes_price,
                mid0=yes_mid,
                ts0=ts,
            )
            active[key] = resting
            by_oid[oid] = resting
            continue

        if ev == "cancel":
            oid = str(e.get("order_id") or "")
            r = by_oid.get(oid)
            if r is not None:
                close(r, ts)

    # Close still-open at last event ts
    if events:
        t_end = float(events[-1].get("ts") or 0.0)
        for r in list(active.values()):
            close(r, t_end)

    market_buckets: dict[str, dict[str, Any]] = {}
    regime_buckets: dict[str, dict[str, Any]] = {}
    markout_sums: dict[str, float] = {f"{int(h)}s": 0.0 for h in MARKOUT_HORIZONS_S}
    markout_ns: dict[str, int] = {f"{int(h)}s": 0 for h in MARKOUT_HORIZONS_S}
    edge_sum = 0.0
    life_sum = 0.0
    n_crossed = 0

    def regime_at(cid: str, ts: float) -> str:
        hist = regime_at_mark.get(cid) or []
        cur = "UNKNOWN"
        for t, reg in hist:
            if t > ts:
                break
            cur = reg
        return cur

    def fv_at_or_after(cid: str, target: float) -> float | None:
        for mts, mfv in marks_by_cid.get(cid, []):
            if mts >= target:
                return mfv
        return None

    for r, ts_end in closed:
        life = max(0.0, ts_end - r.ts0)
        if life <= 0:
            continue
        rep.n_quote_lifetimes += 1
        edge = _edge(r.side, r.price, r.mid0)
        edge_sum += edge
        life_sum += life

        # crossed if any mark while resting pierces price
        was_crossed = False
        for mts, mfv in marks_by_cid.get(r.condition_id, []):
            if mts < r.ts0:
                continue
            if mts > ts_end:
                break
            if _crossed(r.side, r.price, mfv):
                was_crossed = True
                break
        if was_crossed:
            n_crossed += 1

        mb = market_buckets.setdefault(r.condition_id, _close_bucket())
        rb = regime_buckets.setdefault(regime_at(r.condition_id, r.ts0), _close_bucket())
        for b in (mb, rb):
            b["n"] += 1
            b["edge_sum"] += edge
            b["life_sum"] += life
            if was_crossed:
                b["n_crossed"] += 1

        for h in MARKOUT_HORIZONS_S:
            key = f"{int(h)}s"
            target = r.ts0 + h
            if target > ts_end:
                continue
            fv1 = fv_at_or_after(r.condition_id, target)
            if fv1 is None:
                continue
            sm = _signed_move(r.side, r.mid0, fv1)
            markout_sums[key] += sm
            markout_ns[key] += 1
            for b in (mb, rb):
                b["markout_sums"][key] += sm
                b["markout_ns"][key] += 1

    rep.n_crossed = n_crossed
    if rep.n_quote_lifetimes:
        rep.crossed_frac = n_crossed / rep.n_quote_lifetimes
        rep.mean_edge_at_place = edge_sum / rep.n_quote_lifetimes
        rep.mean_lifetime_s = life_sum / rep.n_quote_lifetimes
    for key, n in markout_ns.items():
        rep.markout_n[key] = n
        rep.markout_mean[key] = (markout_sums[key] / n) if n else 0.0

    def finalize(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name, b in raw.items():
            n = int(b["n"])
            if n <= 0:
                continue
            mm = {
                k: round(b["markout_sums"][k] / b["markout_ns"][k], 6)
                if b["markout_ns"][k]
                else 0.0
                for k in b["markout_sums"]
            }
            out[name] = {
                "n": n,
                "crossed_frac": round(b["n_crossed"] / n, 6),
                "mean_edge_at_place": round(b["edge_sum"] / n, 6),
                "mean_lifetime_s": round(b["life_sum"] / n, 6),
                "markout_mean": mm,
                "markout_n": dict(b["markout_ns"]),
            }
        return out

    rep.by_market = finalize(market_buckets)
    rep.by_regime_at_place = finalize(regime_buckets)
    return rep
