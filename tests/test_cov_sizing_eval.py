"""Tests for multi-horizon FV and covariance sizing eval."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.cov_sizing_eval import evaluate_covariance_sizing
from polymaker.replay.fv_calibration import calibrate_fair_value_multi_horizon


def _dual_journal(path: Path, n: int = 100) -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(n):
        ts = t0 + float(i)
        for tok, drift in (("yes-a", 0.0003), ("yes-b", 0.00025)):
            mid = 0.5 + drift * i + (0.002 if i % 7 == 0 else 0.0)
            bid = round(mid - 0.01, 4)
            ask = round(mid + 0.01, 4)
            rows.append({
                "ts": ts,
                "kind": "book",
                "data": {
                    "market": "0xcov",
                    "asset_id": tok,
                    "bids": [{"price": f"{bid:.4f}", "size": "100"}],
                    "asks": [{"price": f"{ask:.4f}", "size": "100"}],
                    "timestamp": str(int(ts * 1000)),
                    "tick_size": "0.001",
                },
            })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_multi_horizon_fv(tmp_path: Path):
    j = tmp_path / "j.jsonl"
    # reuse single-token books for multi-horizon API
    rows = []
    t0 = 1_700_000_000.0
    for i in range(80):
        ts = t0 + i * 2.0
        mid = 0.5 + 0.0004 * i
        bid, ask = round(mid - 0.01, 4), round(mid + 0.01, 4)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xfv",
                "asset_id": "yes-token",
                "bids": [{"price": f"{bid:.4f}", "size": "200"}, {"price": f"{bid-0.01:.4f}", "size": "50"}],
                "asks": [{"price": f"{ask:.4f}", "size": "80"}, {"price": f"{ask+0.01:.4f}", "size": "50"}],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.001",
            },
        })
    j.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    multi = calibrate_fair_value_multi_horizon(
        j, yes_token="yes-token", horizons_s=(5.0, 30.0), sample_every=1, holdout_frac=0.3
    )
    assert "by_horizon" in multi
    assert "5.0" in multi["by_horizon"]


def test_cov_sizing_eval_runs(tmp_path: Path):
    j = tmp_path / "dual.jsonl"
    _dual_journal(j)
    rep = evaluate_covariance_sizing(
        j, token_a="yes-a", token_b="yes-b", holdout_frac=0.3, sample_every=1
    )
    d = rep.as_dict()
    assert d["n_tune"] >= 5
    assert "verdict" in d
