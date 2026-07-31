"""Tests for toxicity calibration and kelly_fraction profile wiring."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.replay.toxicity_calibration import calibrate_toxicity, toxicity_to_prob
from polymaker.strategy.advanced_quoting import AdvancedQuoteInputs, compute_advanced_quotes
from polymaker.domain import MarketMeta, Position, Regime, TokenMeta
from polymaker.marketdata.orderbook import BookView


def test_toxicity_to_prob_bounds():
    assert toxicity_to_prob(0.0) == 0.0
    assert 0.0 < toxicity_to_prob(0.02) < 1.0
    assert toxicity_to_prob(10.0) <= 1.0


def _journal(path: Path, n: int = 80) -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(n):
        ts = t0 + i * 2.0
        mid = 0.5 + 0.0003 * i
        bid, ask = round(mid - 0.01, 4), round(mid + 0.01, 4)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xt",
                "asset_id": "yes-token",
                "bids": [{"price": f"{bid:.4f}", "size": "100"}],
                "asks": [{"price": f"{ask:.4f}", "size": "100"}],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.001",
            },
        })
        if i % 2 == 0:
            rows.append({
                "ts": ts + 0.1,
                "kind": "last_trade_price",
                "data": {
                    "asset_id": "yes-token",
                    "price": f"{mid:.4f}",
                    "size": "50",
                    "side": "BUY" if i % 4 == 0 else "SELL",
                    "timestamp": str(int((ts + 0.1) * 1000)),
                },
            })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_calibrate_toxicity_runs(tmp_path: Path):
    j = tmp_path / "j.jsonl"
    _journal(j)
    rep = calibrate_toxicity(j, yes_token="yes-token", sample_every=1, holdout_frac=0.3)
    assert "toxicity_finding" in rep.as_dict()["verdict"]


def test_kelly_fraction_profile_changes_size():
    meta = MarketMeta(
        condition_id="0x",
        question="q",
        slug="s",
        tokens=(TokenMeta("y", "Yes"), TokenMeta("n", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=5.0,
        rewards_max_spread=5.0,
        rewards_daily_rate=10.0,
        maker_fee_bps=0,
        taker_fee_bps=0,
        fees_enabled=False,
        end_date_iso=None,
        event_id=None,
    )
    view = BookView(
        best_bid=0.48, best_bid_size=100, best_ask=0.52, best_ask_size=100,
        second_bid=0.47, second_ask=0.53, bid_depth=200, ask_depth=200,
    )
    pos = Position(token_id="y", size=0.0)
    base = StrategyProfile(bankroll_usdc=100.0, kelly_fraction=0.25)
    half = StrategyProfile(bankroll_usdc=100.0, kelly_fraction=0.5)

    def _run(p: StrategyProfile):
        return compute_advanced_quotes(
            AdvancedQuoteInputs(
                meta=meta,
                fv=0.5,
                sigma=0.001,
                yes_view=view,
                no_view=view,
                pos_yes=pos,
                pos_no=Position(token_id="n", size=0.0),
                profile=p,
                bankroll_usdc=100.0,
                now=0.0,
                regime=Regime.QUIET,
                toxicity=0.0,
            )
        )

    a = _run(base)
    b = _run(half)
    assert b.size_yes_shares >= a.size_yes_shares
