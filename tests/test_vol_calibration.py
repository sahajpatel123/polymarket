"""Tests for GARCH vs EWMA vol calibration."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.replay.vol_calibration import calibrate_vol_models


def _journal(path: Path, n: int = 150) -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(n):
        ts = t0 + float(i)
        # clustered vol: quiet then noisy
        shock = 0.02 if 60 <= i < 90 else 0.001
        mid = 0.5 + shock * ((-1) ** i) * (i % 3)
        bid = round(mid - 0.01, 4)
        ask = round(mid + 0.01, 4)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xvol",
                "asset_id": "yes-token",
                "bids": [{"price": f"{bid:.4f}", "size": "100"}],
                "asks": [{"price": f"{ask:.4f}", "size": "100"}],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.001",
            },
        })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_vol_calibration_runs(tmp_path: Path):
    j = tmp_path / "j.jsonl"
    _journal(j)
    rep = calibrate_vol_models(
        j, yes_token="yes-token", horizon_s=5.0, sample_every=1, holdout_frac=0.3
    )
    d = rep.as_dict()
    assert "garch" in d and "ewma" in d and "verdict" in d
    assert "garch_finding" in d["verdict"]
