"""Tests for OFI/VPIN signal calibration harness."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.signal_calibration import (
    calibrate_signals,
    ofi_to_prob_up,
    vpin_to_prob_move,
)


def test_ofi_to_prob_mapping():
    assert ofi_to_prob_up(0.0) == 0.5
    assert ofi_to_prob_up(1.0) == 1.0
    assert ofi_to_prob_up(-1.0) == 0.0
    assert 0.5 < ofi_to_prob_up(0.2) < 1.0


def test_vpin_to_prob_clamps():
    assert vpin_to_prob_move(0.0) == 0.0
    assert vpin_to_prob_move(1.5) == 1.0
    assert vpin_to_prob_move(0.4) == 0.4


def _synth_journal(path: Path, n: int = 120) -> None:
    """YES book that drifts up with bid-heavy OFI, plus BUY trades."""
    t0 = 1_700_000_000.0
    yes = "yes-token"
    rows = []
    for i in range(n):
        ts = t0 + float(i) * 2.0
        # Drifting mid upward
        mid = 0.50 + 0.0005 * i
        bid = round(mid - 0.01, 3)
        ask = round(mid + 0.01, 3)
        # Growing bid size → positive OFI
        bid_sz = 100 + 5 * (i % 10)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xqe",
                "asset_id": yes,
                "bids": [
                    {"price": f"{bid:.3f}", "size": str(bid_sz)},
                    {"price": f"{bid - 0.01:.3f}", "size": "50"},
                ],
                "asks": [
                    {"price": f"{ask:.3f}", "size": "80"},
                    {"price": f"{ask + 0.01:.3f}", "size": "50"},
                ],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.001",
            },
        })
        if i % 3 == 0:
            rows.append({
                "ts": ts + 0.1,
                "kind": "last_trade_price",
                "data": {
                    "asset_id": yes,
                    "price": f"{mid:.3f}",
                    "size": "60",
                    "side": "BUY",
                    "timestamp": str(int((ts + 0.1) * 1000)),
                },
            })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_calibrate_signals_runs(tmp_path: Path):
    journal = tmp_path / "j.jsonl"
    _synth_journal(journal)
    rep = calibrate_signals(
        journal,
        yes_token="yes-token",
        horizon_s=10.0,
        sample_every=1,
        holdout_frac=0.3,
    )
    d = rep.as_dict()
    assert d["n_samples"] >= 10
    assert "ofi" in d and "vpin" in d and "verdict" in d
    assert "ofi_finding" in d["verdict"]
