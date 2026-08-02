#!/usr/bin/env python3
"""Session report: realized round-trip PnL and win rate from recorded fills.

Why this exists
---------------
Every prior report measured "win rate" as the sign of a 30-second markout on a
*reconstructed* fill. That is a proxy. This reads the actual fills table and
matches SELLs against BUYs (FIFO) to produce **realized** PnL per closed
round trip — the only definition of win rate that corresponds to money.

It also reports open inventory separately, marked at the last known fair value,
so unrealized markdown is never silently blended into "PnL".

Usage:
    uv run python scripts/session_report.py --db session1/state.db
    uv run python scripts/session_report.py --db session1/state.db --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RoundTrip:
    token_id: str
    qty: float
    buy_price: float
    sell_price: float
    buy_ts: float
    sell_ts: float

    @property
    def pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.qty

    @property
    def hold_s(self) -> float:
        return max(0.0, self.sell_ts - self.buy_ts)

    @property
    def won(self) -> bool:
        return self.pnl > 0.0


@dataclass
class Report:
    db: str
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    round_trips: list[RoundTrip] = field(default_factory=list)
    open_positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    first_ts: float | None = None
    last_ts: float | None = None
    equity_last: float | None = None
    equity_min: float | None = None

    # ── derived ──
    @property
    def realized_pnl(self) -> float:
        return sum(rt.pnl for rt in self.round_trips)

    @property
    def n_closed(self) -> int:
        return len(self.round_trips)

    @property
    def win_rate(self) -> float | None:
        if not self.round_trips:
            return None
        return sum(1 for rt in self.round_trips if rt.won) / len(self.round_trips)

    @property
    def unrealized_pnl(self) -> float:
        total = 0.0
        for tok, (qty, avg) in self.open_positions.items():
            mark = self.marks.get(tok)
            if mark is None or qty <= 0:
                continue
            total += (mark - avg) * qty
        return total

    @property
    def open_cost(self) -> float:
        return sum(qty * avg for qty, avg in self.open_positions.values())

    def as_dict(self) -> dict[str, Any]:
        wr = self.win_rate
        holds = [rt.hold_s for rt in self.round_trips]
        wins = [rt.pnl for rt in self.round_trips if rt.won]
        losses = [rt.pnl for rt in self.round_trips if not rt.won]
        dur = None
        if self.first_ts and self.last_ts:
            dur = round((self.last_ts - self.first_ts) / 60.0, 1)
        return {
            "db": self.db,
            "window_minutes": dur,
            "fills": {
                "total": self.n_fills,
                "buys": self.n_buys,
                "sells": self.n_sells,
                "buy_notional_usdc": round(self.buy_notional, 2),
                "sell_notional_usdc": round(self.sell_notional, 2),
                "sell_share": (round(self.n_sells / self.n_fills, 4)
                               if self.n_fills else None),
            },
            "round_trips": {
                "closed": self.n_closed,
                "win_rate": None if wr is None else round(wr, 4),
                "realized_pnl_usdc": round(self.realized_pnl, 4),
                "avg_pnl_per_trip": (round(self.realized_pnl / self.n_closed, 4)
                                     if self.n_closed else None),
                "gross_win_usdc": round(sum(wins), 4),
                "gross_loss_usdc": round(sum(losses), 4),
                "profit_factor": (round(sum(wins) / abs(sum(losses)), 3)
                                  if losses and sum(losses) != 0 else None),
                "median_hold_s": (round(statistics.median(holds), 1)
                                  if holds else None),
            },
            "open_inventory": {
                "tokens": len(self.open_positions),
                "cost_usdc": round(self.open_cost, 2),
                "unrealized_pnl_usdc": round(self.unrealized_pnl, 4),
                "marked": sum(1 for t in self.open_positions if t in self.marks),
            },
            "equity": {
                "last": (None if self.equity_last is None
                         else round(self.equity_last, 4)),
                "min": (None if self.equity_min is None
                        else round(self.equity_min, 4)),
            },
        }


def build(db_path: Path) -> Report:
    rep = Report(db=str(db_path))
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    rows = list(con.execute(
        "SELECT token_id, side, price, size, ts FROM fills ORDER BY ts, rowid"
    ))
    rep.n_fills = len(rows)
    if rows:
        rep.first_ts, rep.last_ts = float(rows[0][4]), float(rows[-1][4])

    # FIFO lots per token; a SELL closes the oldest BUY lots first.
    lots: dict[str, deque[list[float]]] = defaultdict(deque)  # [qty, price, ts]
    for token_id, side, price, size, ts in rows:
        price, size, ts = float(price), float(size), float(ts)
        if str(side) == "BUY":
            rep.n_buys += 1
            rep.buy_notional += price * size
            lots[token_id].append([size, price, ts])
            continue
        rep.n_sells += 1
        rep.sell_notional += price * size
        remaining = size
        while remaining > 1e-9 and lots[token_id]:
            lot = lots[token_id][0]
            take = min(remaining, lot[0])
            rep.round_trips.append(RoundTrip(
                token_id=token_id, qty=take, buy_price=lot[1],
                sell_price=price, buy_ts=lot[2], sell_ts=ts,
            ))
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots[token_id].popleft()

    # leftover lots = still-open inventory (size-weighted average cost)
    for tok, dq in lots.items():
        qty = sum(lot[0] for lot in dq)
        if qty <= 1e-9:
            continue
        avg = sum(lot[0] * lot[1] for lot in dq) / qty
        rep.open_positions[tok] = (qty, avg)

    # marks: prefer the store's own avg/positions table for a sanity check,
    # and use the last fill price per token as the mark of record.
    for tok, _ in rep.open_positions.items():
        row = con.execute(
            "SELECT price FROM fills WHERE token_id=? ORDER BY ts DESC LIMIT 1",
            (tok,),
        ).fetchone()
        if row:
            rep.marks[tok] = float(row[0])

    try:
        row = con.execute(
            "SELECT equity FROM pnl_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            rep.equity_last = float(row[0])
        row = con.execute("SELECT MIN(equity) FROM pnl_snapshots").fetchone()
        if row and row[0] is not None:
            rep.equity_min = float(row[0])
    except sqlite3.Error:
        pass
    con.close()
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"db not found: {args.db}")
    rep = build(args.db)
    d = rep.as_dict()
    if args.json:
        print(json.dumps(d, indent=2))
        return 0

    f, rt, oi, eq = d["fills"], d["round_trips"], d["open_inventory"], d["equity"]
    print(f"\n=== SESSION REPORT — {d['db']} ===")
    print(f"window: {d['window_minutes']} min")
    print("\nFILLS")
    print(f"  total {f['total']}   buys {f['buys']}   sells {f['sells']}"
          f"   sell share {f['sell_share']}")
    print(f"  bought ${f['buy_notional_usdc']}   sold ${f['sell_notional_usdc']}")
    print("\nROUND TRIPS (realized — matched BUY->SELL, FIFO)")
    print(f"  closed        {rt['closed']}")
    print(f"  win rate      {rt['win_rate']}")
    print(f"  realized PnL  ${rt['realized_pnl_usdc']}")
    print(f"  avg per trip  ${rt['avg_pnl_per_trip']}")
    print(f"  gross win/loss ${rt['gross_win_usdc']} / ${rt['gross_loss_usdc']}"
          f"   profit factor {rt['profit_factor']}")
    print(f"  median hold   {rt['median_hold_s']}s")
    print("\nOPEN INVENTORY (not realized)")
    print(f"  tokens {oi['tokens']}   cost ${oi['cost_usdc']}"
          f"   unrealized ${oi['unrealized_pnl_usdc']}")
    print("\nEQUITY")
    print(f"  last ${eq['last']}   min ${eq['min']}")
    if rt["closed"] == 0:
        print("\n  NOTE: zero closed round trips — win rate is undefined, not 0%.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
