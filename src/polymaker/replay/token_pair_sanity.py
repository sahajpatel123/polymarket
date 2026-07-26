"""Token-pair sanity: YES+NO mids should sum ≈ 1 for a binary market.

Mis-paired tokens (e.g. Vance NO treated as Newsom YES) make 1−fv disagree
with the NO book, so construct_quotes drops the NO side and AS EV cannot bind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_price_changes
from polymaker.replay import filter_rows_for_tokens, load_journal


@dataclass(frozen=True)
class TokenPairSanity:
    n_samples: int
    mean_sum: float | None
    min_sum: float | None
    max_sum: float | None
    frac_near_one: float | None  # |sum-1| <= tol
    pair_ok: bool
    reason: str
    tol: float
    yes_token: str
    no_token: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_samples": self.n_samples,
            "mean_sum": None if self.mean_sum is None else round(self.mean_sum, 6),
            "min_sum": None if self.min_sum is None else round(self.min_sum, 6),
            "max_sum": None if self.max_sum is None else round(self.max_sum, 6),
            "frac_near_one": (
                None if self.frac_near_one is None else round(self.frac_near_one, 6)
            ),
            "pair_ok": self.pair_ok,
            "reason": self.reason,
            "tol": self.tol,
            "yes_token": self.yes_token,
            "no_token": self.no_token,
        }


def assess_token_pair(
    journal: Path,
    yes_token: str,
    no_token: str,
    *,
    tol: float = 0.02,
    sample_every: int = 1,
) -> TokenPairSanity:
    """Replay books and score mean(YES mid + NO mid)."""
    rows = filter_rows_for_tokens(
        load_journal(journal), yes_token=yes_token, no_token=no_token
    )
    yb = OrderBook()
    nb = OrderBook()
    sums: list[float] = []
    n = 0
    for row in rows:
        kind = row.get("kind")
        data = row.get("data")
        ts = float(row.get("ts") or 0.0)
        if not isinstance(data, dict):
            continue
        if kind == "book":
            upd = parse_book(data)
            if upd is None:
                continue
            book = yb if upd.asset_id == yes_token else nb if upd.asset_id == no_token else None
            if book is None:
                continue
            if upd.tick_size:
                book.set_tick_size(upd.tick_size)
            book.apply_snapshot(upd.bids, upd.asks, upd.ts or ts, upd.book_hash)
        elif kind == "price_change":
            for ch in parse_price_changes(data):
                book = (
                    yb
                    if ch.asset_id == yes_token
                    else nb
                    if ch.asset_id == no_token
                    else None
                )
                if book is None:
                    continue
                book.apply_delta(ch.side, ch.price, ch.size, ch.ts or ts)
        else:
            continue
        n += 1
        if sample_every > 1 and n % sample_every != 0:
            continue
        yv, nv = yb.view(), nb.view()
        if (
            yv.best_bid is None
            or yv.best_ask is None
            or nv.best_bid is None
            or nv.best_ask is None
        ):
            continue
        if yv.best_bid >= yv.best_ask or nv.best_bid >= nv.best_ask:
            continue
        ym = 0.5 * (yv.best_bid + yv.best_ask)
        nm = 0.5 * (nv.best_bid + nv.best_ask)
        sums.append(ym + nm)

    if not sums:
        return TokenPairSanity(
            n_samples=0,
            mean_sum=None,
            min_sum=None,
            max_sum=None,
            frac_near_one=None,
            pair_ok=False,
            reason="no_overlapping_book_samples",
            tol=tol,
            yes_token=yes_token,
            no_token=no_token,
        )

    mean_s = sum(sums) / len(sums)
    near = sum(1 for s in sums if abs(s - 1.0) <= tol) / len(sums)
    ok = near >= 0.95 and abs(mean_s - 1.0) <= tol
    reason = "ok" if ok else f"mean_sum={mean_s:.4f} frac_near_one={near:.3f}"
    return TokenPairSanity(
        n_samples=len(sums),
        mean_sum=mean_s,
        min_sum=min(sums),
        max_sum=max(sums),
        frac_near_one=near,
        pair_ok=ok,
        reason=reason,
        tol=tol,
        yes_token=yes_token,
        no_token=no_token,
    )


def write_token_pair_sanity(report: TokenPairSanity, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
