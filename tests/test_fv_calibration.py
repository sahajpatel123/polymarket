"""Tests for fair-value predictor calibration."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.fv_calibration import calibrate_fair_value


def _journal(path: Path, n: int = 120) -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(n):
        ts = t0 + float(i) * 2.0
        mid = 0.50 + 0.0004 * i
        bid = round(mid - 0.01, 4)
        ask = round(mid + 0.01, 4)
        # Skewed book → microprice ≠ mid
        bid_sz = 200 + 10 * (i % 5)
        ask_sz = 80
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xfv",
                "asset_id": "yes-token",
                "bids": [
                    {"price": f"{bid:.4f}", "size": str(bid_sz)},
                    {"price": f"{bid - 0.01:.4f}", "size": "50"},
                ],
                "asks": [
                    {"price": f"{ask:.4f}", "size": str(ask_sz)},
                    {"price": f"{ask + 0.01:.4f}", "size": "50"},
                ],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.001",
            },
        })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_fv_calibration_runs(tmp_path: Path):
    j = tmp_path / "j.jsonl"
    _journal(j)
    rep = calibrate_fair_value(
        j, yes_token="yes-token", horizon_s=10.0, sample_every=1, holdout_frac=0.3
    )
    d = rep.as_dict()
    assert d["n"] >= 5
    assert "micro_finding" in d["verdict"]
    assert "mid" in d["predictors"]
