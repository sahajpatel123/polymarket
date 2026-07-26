"""Quote–trade gap diagnostic (why optimistic n_fill can be 0).

Maker path posts BUY-YES / BUY-NO only. Fills require SELL aggressors at
prices <= our bids. If bids sit systematically below the tape, trades never
cross even under optimistic matching — AS EV stays unbound.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Side
from polymaker.marketdata.parse import parse_last_trade
from polymaker.metrics import MetricsLogger
from polymaker.replay import ReplayState, apply_journal_event, _recompute


@dataclass(frozen=True)
class QuoteTradeGap:
    n_trades: int
    n_trades_with_live: int
    n_aggressor_buy: int
    n_aggressor_sell: int
    n_crossable: int
    n_fill: int
    n_quote: int
    median_bid_gap: float | None  # trade_price - best_bid (positive => bid below tape)
    mean_bid_gap: float | None
    p90_bid_gap: float | None
    mean_trade_minus_fv: float | None = None
    median_trade_minus_fv: float | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "n_trades_with_live": self.n_trades_with_live,
            "n_aggressor_buy": self.n_aggressor_buy,
            "n_aggressor_sell": self.n_aggressor_sell,
            "n_crossable": self.n_crossable,
            "n_fill": self.n_fill,
            "n_quote": self.n_quote,
            "median_bid_gap": (
                None if self.median_bid_gap is None else round(self.median_bid_gap, 6)
            ),
            "mean_bid_gap": (
                None if self.mean_bid_gap is None else round(self.mean_bid_gap, 6)
            ),
            "p90_bid_gap": (
                None if self.p90_bid_gap is None else round(self.p90_bid_gap, 6)
            ),
            "mean_trade_minus_fv": (
                None
                if self.mean_trade_minus_fv is None
                else round(self.mean_trade_minus_fv, 6)
            ),
            "median_trade_minus_fv": (
                None
                if self.median_trade_minus_fv is None
                else round(self.median_trade_minus_fv, 6)
            ),
            "reason": self.reason,
        }


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, max(0, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def measure_quote_trade_gap(
    rows: list[dict[str, Any]],
    meta: MarketMeta,
    profile: StrategyProfile,
    *,
    metrics_path: str | None = None,
) -> QuoteTradeGap:
    """Replay with optimistic fills and measure bid-vs-tape gaps at each trade."""
    import tempfile

    if metrics_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        metrics_path = tmp.name
        tmp.close()

    st = ReplayState(meta=meta, profile=profile, fill_mode="optimistic")
    st.metrics = MetricsLogger(Path(metrics_path), enabled=True)

    n_trades = 0
    n_with_live = 0
    n_buy = 0
    n_sell = 0
    n_crossable = 0
    gaps: list[float] = []
    fv_gaps: list[float] = []  # trade - token FV

    for row in rows:
        kind = row.get("kind")
        data = row.get("data")
        ts = float(row.get("ts") or 0.0)
        if kind == "last_trade_price" and isinstance(data, dict):
            tp = parse_last_trade(data)
            if tp is not None and tp.asset_id in (
                meta.yes.token_id,
                meta.no.token_id,
            ):
                n_trades += 1
                if tp.aggressor is Side.BUY:
                    n_buy += 1
                else:
                    n_sell += 1
                live_same = [
                    o for o in st.live.values() if o.token_id == tp.asset_id
                ]
                # Token FV for YES book; for NO use 1-fv
                if st.est is not None and st.est.last_fv is not None:
                    if tp.asset_id == meta.yes.token_id:
                        fv_tok = float(st.est.last_fv)
                    else:
                        fv_tok = 1.0 - float(st.est.last_fv)
                    fv_gaps.append(float(tp.price) - fv_tok)
                if live_same:
                    n_with_live += 1
                    bids = [o.price for o in live_same if o.side is Side.BUY]
                    if bids:
                        best_bid = max(bids)
                        gaps.append(float(tp.price) - best_bid)
                    for o in live_same:
                        if (
                            o.side is Side.BUY
                            and tp.aggressor is Side.SELL
                            and tp.price <= o.price + 1e-12
                        ):
                            n_crossable += 1
                        if (
                            o.side is Side.SELL
                            and tp.aggressor is Side.BUY
                            and tp.price >= o.price - 1e-12
                        ):
                            n_crossable += 1

        if apply_journal_event(st, row):
            _recompute(st, ts)

    st.metrics.close()
    try:
        Path(metrics_path).unlink(missing_ok=True)
    except OSError:
        pass

    gaps_sorted = sorted(gaps)
    mean_gap = (sum(gaps) / len(gaps)) if gaps else None
    med = _percentile(gaps_sorted, 0.5)
    p90 = _percentile(gaps_sorted, 0.9)
    mean_fv_gap = (sum(fv_gaps) / len(fv_gaps)) if fv_gaps else None
    med_fv_gap = _percentile(sorted(fv_gaps), 0.5) if fv_gaps else None

    reasons: list[str] = []
    if n_trades == 0:
        reasons.append("no_trades")
    elif n_with_live == 0:
        reasons.append("no_live_quotes_at_trade")
    elif n_crossable == 0 and mean_gap is not None and mean_gap > 0:
        reasons.append(f"bids_below_tape_mean_gap={mean_gap:.4f}")
        if mean_fv_gap is not None:
            reasons.append(f"mean_trade_minus_fv={mean_fv_gap:.4f}")
    elif n_crossable == 0 and n_sell == 0:
        reasons.append("no_sell_aggressors_for_bid_fills")
    elif n_crossable == 0:
        reasons.append("no_crossable_quotes")
    else:
        reasons.append("ok")

    return QuoteTradeGap(
        n_trades=n_trades,
        n_trades_with_live=n_with_live,
        n_aggressor_buy=n_buy,
        n_aggressor_sell=n_sell,
        n_crossable=n_crossable,
        n_fill=st.n_fill,
        n_quote=st.n_quote,
        median_bid_gap=med,
        mean_bid_gap=mean_gap,
        p90_bid_gap=p90,
        mean_trade_minus_fv=mean_fv_gap,
        median_trade_minus_fv=med_fv_gap,
        reason=";".join(reasons),
    )
