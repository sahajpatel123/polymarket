"""Tests for micro_levels sweep and flow_z calibration."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.flow_calibration import calibrate_flow, flow_z_to_prob_up
from polymaker.replay.fv_calibration import sweep_micro_levels


def test_flow_z_mapping():
    assert flow_z_to_prob_up(0.0) == 0.5
    assert flow_z_to_prob_up(1.0) == 1.0
    assert flow_z_to_prob_up(-1.0) == 0.0


def _journal(path: Path, n: int = 100) -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(n):
        ts = t0 + float(i) * 2.0
        mid = 0.50 + 0.0004 * i
        bid, ask = round(mid - 0.01, 4), round(mid + 0.01, 4)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xf",
                "asset_id": "yes-token",
                "bids": [
                    {"price": f"{bid:.4f}", "size": str(150 + i % 20)},
                    {"price": f"{bid - 0.01:.4f}", "size": "40"},
                ],
                "asks": [
                    {"price": f"{ask:.4f}", "size": "80"},
                    {"price": f"{ask + 0.01:.4f}", "size": "40"},
                ],
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
                    "size": "40",
                    "side": "BUY",
                    "timestamp": str(int((ts + 0.1) * 1000)),
                },
            })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_sweep_micro_levels(tmp_path: Path):
    j = tmp_path / "j.jsonl"
    _journal(j)
    sweep = sweep_micro_levels(
        j, yes_token="yes-token", levels=(1, 3), horizon_s=10.0, sample_every=1, holdout_frac=0.3
    )
    assert len(sweep["rows"]) == 2
    assert "any_finding" in sweep


def test_calibrate_flow_runs(tmp_path: Path):
    j = tmp_path / "j.jsonl"
    _journal(j)
    rep = calibrate_flow(
        j, yes_token="yes-token", horizon_s=10.0, sample_every=1, holdout_frac=0.3
    )
    assert "flow_finding" in rep.as_dict()["verdict"]
