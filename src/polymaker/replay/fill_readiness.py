"""Tape fill-readiness gate for adverse-selection EV claims.

Political long-dated paper journals often have many book updates but almost
no `last_trade_price` prints. Without trades, the fill simulator cannot bind
adverse selection, so EV ablations collapse to reward-path noise.

This module scores journal trade density (and optional optimistic replay
fill counts) so quant_edge can mark `as_ev_ready` explicitly.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta
from polymaker.replay import filter_rows_for_tokens, load_journal, run_replay
from polymaker.replay.compare import write_sliced_journal


@dataclass(frozen=True)
class FillReadiness:
    n_events: int
    n_trades: int
    n_book: int
    n_price_change: int
    trades_per_hour: float
    duration_s: float
    n_fill_optimistic: int | None
    n_quote_optimistic: int | None
    as_ev_ready: bool
    reason: str
    min_trades: int
    min_fills_optimistic: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_events": self.n_events,
            "n_trades": self.n_trades,
            "n_book": self.n_book,
            "n_price_change": self.n_price_change,
            "trades_per_hour": round(self.trades_per_hour, 4),
            "duration_s": round(self.duration_s, 3),
            "n_fill_optimistic": self.n_fill_optimistic,
            "n_quote_optimistic": self.n_quote_optimistic,
            "as_ev_ready": self.as_ev_ready,
            "reason": self.reason,
            "min_trades": self.min_trades,
            "min_fills_optimistic": self.min_fills_optimistic,
        }


def count_journal_kinds(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        k = str(row.get("kind") or "")
        out[k] = out.get(k, 0) + 1
    return out


def assess_fill_readiness(
    journal: Path,
    meta: MarketMeta | None = None,
    *,
    profile: StrategyProfile | None = None,
    min_trades: int = 50,
    min_fills_optimistic: int = 20,
    run_optimistic_probe: bool = False,
) -> FillReadiness:
    """Assess whether a journal can support adverse-selection EV claims."""
    rows = load_journal(journal)
    if meta is not None:
        yes_id = meta.yes.token_id
        no_id = meta.no.token_id
        if yes_id not in ("yes-token", "") and no_id not in ("no-token", ""):
            filtered = filter_rows_for_tokens(rows, yes_token=yes_id, no_token=no_id)
            if filtered:
                rows = filtered

    kinds = count_journal_kinds(rows)
    n_trades = int(kinds.get("last_trade_price", 0))
    n_book = int(kinds.get("book", 0))
    n_pc = int(kinds.get("price_change", 0))
    if rows:
        t0 = float(rows[0].get("ts") or 0.0)
        t1 = float(rows[-1].get("ts") or 0.0)
        duration = max(0.0, t1 - t0)
    else:
        duration = 0.0
    tph = (n_trades / duration * 3600.0) if duration > 0 else 0.0

    n_fill: int | None = None
    n_quote: int | None = None
    if run_optimistic_probe and meta is not None and profile is not None:
        with tempfile.TemporaryDirectory(prefix="fill_ready_") as td:
            root = Path(td)
            jpath = write_sliced_journal(rows, root / "journal.jsonl")
            metrics = root / "metrics.jsonl"
            result = run_replay(
                jpath, meta, profile, metrics, fill_mode="optimistic"
            )
            n_fill = int(result.n_fill)
            n_quote = int(result.n_quote)

    reasons: list[str] = []
    ready = True
    if n_trades < min_trades:
        ready = False
        reasons.append(f"n_trades={n_trades}<{min_trades}")
    if run_optimistic_probe:
        if n_fill is None or n_fill < min_fills_optimistic:
            ready = False
            reasons.append(f"n_fill_optimistic={n_fill}<{min_fills_optimistic}")
    if not reasons:
        reasons.append("ok")

    return FillReadiness(
        n_events=len(rows),
        n_trades=n_trades,
        n_book=n_book,
        n_price_change=n_pc,
        trades_per_hour=tph,
        duration_s=duration,
        n_fill_optimistic=n_fill,
        n_quote_optimistic=n_quote,
        as_ev_ready=ready,
        reason=";".join(reasons),
        min_trades=min_trades,
        min_fills_optimistic=min_fills_optimistic,
    )


def write_fill_readiness(report: FillReadiness, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
