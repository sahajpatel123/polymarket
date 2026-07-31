"""Tests for Quantitative Edge evidence harness + new pure modules."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import profile_from_overrides
from polymaker.replay.quant_edge import TECHNIQUE_INVENTORY, evaluate_quant_edge
from polymaker.strategy.garch import GARCHVolatility
from polymaker.strategy.signal_blend import (
    SignalSource,
    blend_probabilities,
    calibration_weight,
)


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xreplay",
        question="qe",
        slug="qe",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def _journal(path: Path, n: int = 80) -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(n):
        ts = t0 + float(i) * 2.0
        mid_bid = 0.48 + 0.001 * ((i % 7) - 3)
        mid_ask = 0.52 + 0.001 * ((i % 5) - 2)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xreplay",
                "asset_id": "yes-token",
                "bids": [
                    {"price": f"{mid_bid:.3f}", "size": "500"},
                    {"price": f"{mid_bid - 0.01:.3f}", "size": "400"},
                ],
                "asks": [
                    {"price": f"{mid_ask:.3f}", "size": "500"},
                    {"price": f"{mid_ask + 0.01:.3f}", "size": "400"},
                ],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.01",
            },
        })
        rows.append({
            "ts": ts + 0.1,
            "kind": "book",
            "data": {
                "market": "0xreplay",
                "asset_id": "no-token",
                "bids": [
                    {"price": f"{1 - mid_ask:.3f}", "size": "500"},
                    {"price": f"{1 - mid_ask - 0.01:.3f}", "size": "400"},
                ],
                "asks": [
                    {"price": f"{1 - mid_bid:.3f}", "size": "500"},
                    {"price": f"{1 - mid_bid + 0.01:.3f}", "size": "400"},
                ],
                "timestamp": str(int((ts + 0.1) * 1000)),
                "tick_size": "0.01",
            },
        })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_calibration_weight_extremes():
    assert calibration_weight(0.0) == 1.0
    assert calibration_weight(0.25) == 0.0
    assert calibration_weight(0.40) == 0.0
    assert 0.0 < calibration_weight(0.10) < 1.0


def test_signal_blend_prefers_better_calibrated():
    sources = (
        SignalSource("book", 0.60, brier_score=0.05),
        SignalSource("external", 0.90, brier_score=0.22),
    )
    res = blend_probabilities(sources)
    # Book dominates → blend closer to 0.60 than 0.90
    assert abs(res.probability - 0.60) < abs(res.probability - 0.90)
    weights = dict(res.weights)
    assert weights["book"] > weights["external"]


def test_signal_blend_uninformative_fallback():
    sources = (
        SignalSource("a", 0.40, brier_score=0.30),
        SignalSource("b", 0.60, brier_score=0.30),
    )
    res = blend_probabilities(sources)
    assert res.probability == 0.5


def test_garch_tracks_vol_cluster():
    g = GARCHVolatility(omega=1e-6, alpha=0.1, beta=0.8)
    # Quiet returns
    for _ in range(20):
        g.update(0.001)
    quiet = g.sigma
    # Shock cluster
    for _ in range(5):
        g.update(0.05)
    shocked = g.sigma
    assert shocked > quiet
    assert g.unconditional_variance() > 0


def test_technique_inventory_covers_core_set():
    ids = {t["id"] for t in TECHNIQUE_INVENTORY}
    for required in (
        "microprice",
        "avellaneda_stoikov",
        "kelly_fractional",
        "vpin",
        "kyle_lambda",
        "ofi_skew",
        "garch_vol",
        "signal_blend_calibration",
        "covariance_sizing",
        "markout_toxicity",
    ):
        assert required in ids
    by_id = {t["id"]: t for t in TECHNIQUE_INVENTORY}
    # Fed into estimators; kyle also has opt-in c_kyle quote path (default off)
    assert by_id["vpin"]["wired"] == "fed"
    assert by_id["ofi_skew"]["wired"] == "fed"
    assert by_id["vpin"]["evidence"] == "no"
    assert by_id["ofi_skew"]["evidence"] == "no"
    assert by_id["kyle_lambda"]["evidence"] == "partial"
    assert by_id["kyle_lambda"]["wired"] == "fed+opt-in"
    assert by_id["microprice"]["evidence"] == "no"
    assert by_id["markout_toxicity"]["evidence"] == "no"
    assert "flow_nudge_fv" in by_id
    assert by_id["flow_nudge_fv"]["evidence"] == "no"
    assert "join_best_bid" in by_id
    assert by_id["join_best_bid"]["wired"] == "opt-in"
    assert by_id["join_best_bid"]["evidence"] == "no"


def test_quant_edge_eval_runs(tmp_path: Path):
    journal = tmp_path / "j.jsonl"
    _journal(journal, n=60)
    baseline = StrategyProfile()
    candidate = profile_from_overrides(baseline, {"use_as_reservation_price": True})
    result = evaluate_quant_edge(
        journal,
        _meta(),
        baseline,
        candidate,
        tmp_path / "out",
        holdout_frac=0.3,
        split="events",
        n_chunks=3,
    )
    d = result.as_dict()
    assert "full" in d and "holdout" in d and "significance" in d
    assert "verdict" in d
    assert "finding" in d["verdict"]
    assert "ev_signal" in d["verdict"]
    assert "reward_accrual_delta" in d["verdict"]
    assert d["verdict"]["fill_mode"] == "conservative"
    assert "promotion_eligible" in d["verdict"]
    if d["verdict"]["n_fill_candidate"] == 0:
        assert d["verdict"]["finding"] is False
    assert d["significance"]["n_chunks"] >= 1
    # Calibration keys present on deltas
    assert "brier_score" in d["full"]["delta"]
    assert "ev_per_quote_usdc" in d["full"]["delta"]


def test_quant_edge_eval_fill_mode_optimistic(tmp_path: Path):
    journal = tmp_path / "j.jsonl"
    _journal(journal, n=60)
    baseline = StrategyProfile()
    candidate = profile_from_overrides(baseline, {"use_as_reservation_price": True})
    result = evaluate_quant_edge(
        journal,
        _meta(),
        baseline,
        candidate,
        tmp_path / "out_opt",
        holdout_frac=0.3,
        split="events",
        n_chunks=3,
        fill_mode="optimistic",
    )
    d = result.as_dict()
    assert d["verdict"]["fill_mode"] == "optimistic"
    # Diagnostic modes cannot be promotion-eligible even if finding flips true.
    if d["verdict"]["finding"]:
        assert d["verdict"]["promotion_eligible"] is False
    else:
        assert d["verdict"]["promotion_eligible"] is False
    assert d["full"]["window"].get("fill_mode") == "optimistic"
