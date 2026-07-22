"""Deterministic multi-regime synthetic journals for strategy eval.

Produces quiet → jump → recovery book tapes so Tier-2 ideas can be scored
offline before paper data exists. Pure helpers; no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

RegimeName = Literal["quiet", "jump", "recovery"]


def _book_row(
    *,
    ts: float,
    market: str,
    asset_id: str,
    bid: float,
    ask: float,
    size: float = 500.0,
    tick: float = 0.01,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": "book",
        "data": {
            "market": market,
            "asset_id": asset_id,
            "bids": [
                {"price": f"{bid:.4f}", "size": f"{size:.0f}"},
                {"price": f"{bid - tick:.4f}", "size": f"{size * 0.8:.0f}"},
            ],
            "asks": [
                {"price": f"{ask:.4f}", "size": f"{size:.0f}"},
                {"price": f"{ask + tick:.4f}", "size": f"{size * 0.8:.0f}"},
            ],
            "timestamp": str(int(ts * 1000)),
            "tick_size": f"{tick}",
        },
    }


def _trade_row(
    *,
    ts: float,
    market: str,
    asset_id: str,
    price: float,
    size: float,
    side: str = "BUY",
) -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": "last_trade_price",
        "data": {
            "market": market,
            "asset_id": asset_id,
            "price": f"{price:.4f}",
            "size": f"{size:.0f}",
            "side": side,
            "timestamp": str(int(ts * 1000)),
        },
    }


def generate_regime_journal(
    *,
    yes_token: str = "yes-token",
    no_token: str = "no-token",
    market: str = "0xreplay",
    tick: float = 0.01,
    t0: float = 1_700_000_000.0,
    quiet_steps: int = 8,
    jump_ticks: int = 10,
    recovery_steps: int = 6,
    cycles: int = 1,
) -> list[dict[str, Any]]:
    """Build quiet → toxic jump → recovery journal(s).

    Quiet: tight 0.48/0.52 book, small prints.
    Jump: bid/ask gap up by jump_ticks with a large aggressor print (EVENT-like).
    Recovery: book walks back toward mid with moderate flow.

    cycles>1 repeats the pattern so offline OOS holdouts are not quote-thin
    (T1-38 dense synth for C-01-style validators).
    """
    rows: list[dict[str, Any]] = []
    bid, ask = 0.48, 0.52
    ts = t0
    n_cycles = max(1, int(cycles))

    def emit_two_sided(b: float, a: float, trade_size: float | None = None) -> None:
        nonlocal ts
        rows.append(
            _book_row(ts=ts, market=market, asset_id=yes_token, bid=b, ask=a, tick=tick)
        )
        rows.append(
            _book_row(
                ts=ts + 0.01,
                market=market,
                asset_id=no_token,
                bid=round(1.0 - a, 4),
                ask=round(1.0 - b, 4),
                tick=tick,
            )
        )
        if trade_size is not None:
            mid = (b + a) / 2.0
            rows.append(
                _trade_row(
                    ts=ts + 0.05,
                    market=market,
                    asset_id=yes_token,
                    price=mid,
                    size=trade_size,
                    side="BUY",
                )
            )
        ts += 1.0

    for _ in range(n_cycles):
        for i in range(quiet_steps):
            # 1-tick micro-jitter: sticky reprice_ticks should ignore these flaps.
            wobble = tick * ((i % 3) - 1)
            emit_two_sided(
                bid + wobble, ask + wobble, trade_size=15.0 if i % 2 == 0 else None
            )

        # Toxic jump: large print + book displaces by jump_ticks
        jump = jump_ticks * tick
        bid_j, ask_j = bid + jump, ask + jump
        emit_two_sided(bid_j, ask_j, trade_size=800.0)

        for i in range(recovery_steps):
            frac = (i + 1) / max(recovery_steps, 1)
            b = bid_j - jump * frac * 0.7
            a = ask_j - jump * frac * 0.7
            emit_two_sided(b, a, trade_size=40.0)

        # Settle back near start mid between cycles
        bid, ask = 0.48, 0.52

    return rows


def write_regime_journal(path: Path, **kwargs: Any) -> dict[str, Any]:
    rows = generate_regime_journal(**kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
    return {"path": str(path), "n_events": len(rows), "ts_start": rows[0]["ts"], "ts_end": rows[-1]["ts"]}
