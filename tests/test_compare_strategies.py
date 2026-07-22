"""Tests for strategy A/B compare harness (eval infra for Tier-2)."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.replay.compare import (
    compare_profiles,
    profile_from_overrides,
    slice_journal_rows,
)


def _journal_fixture(path: Path, yes: str = "yes-token", no: str = "no-token",
                     market: str = "0xreplay") -> None:
    t0 = 1_700_000_000.0
    rows = []
    for i in range(10):
        ts = t0 + float(i)
        mid_bid = 0.48 - 0.001 * (i % 3)
        mid_ask = 0.52 + 0.001 * (i % 3)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": market,
                "asset_id": yes,
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
            "ts": ts + 0.05,
            "kind": "book",
            "data": {
                "market": market,
                "asset_id": no,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.47", "size": "400"}],
                "asks": [{"price": "0.52", "size": "500"}, {"price": "0.53", "size": "400"}],
                "timestamp": str(int((ts + 0.05) * 1000)),
                "tick_size": "0.01",
            },
        })
        if i % 2 == 0:
            rows.append({
                "ts": ts + 0.2,
                "kind": "last_trade_price",
                "data": {
                    "market": market,
                    "asset_id": yes,
                    "price": "0.50",
                    "size": "20",
                    "side": "BUY",
                    "timestamp": str(int((ts + 0.2) * 1000)),
                },
            })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_slice_journal_holdout_splits_timeline() -> None:
    rows = [{"ts": float(i), "kind": "book", "data": {}} for i in range(10)]
    tune, w_tune = slice_journal_rows(rows, holdout_frac=0.3, use_holdout=False)
    hold, w_hold = slice_journal_rows(rows, holdout_frac=0.3, use_holdout=True)
    assert w_tune["mode"] == "tune"
    assert w_hold["mode"] == "holdout"
    assert len(tune) + len(hold) == len(rows)
    assert max(float(r["ts"]) for r in tune) < min(float(r["ts"]) for r in hold)


def test_slice_journal_events_split_balances_counts() -> None:
    rows = [{"ts": float(i), "kind": "book", "data": {}} for i in range(10)]
    tune, w_tune = slice_journal_rows(
        rows, holdout_frac=0.3, use_holdout=False, split="events"
    )
    hold, w_hold = slice_journal_rows(
        rows, holdout_frac=0.3, use_holdout=True, split="events"
    )
    assert w_tune["mode"] == "tune_events"
    assert w_hold["mode"] == "holdout_events"
    assert len(tune) + len(hold) == len(rows)
    assert len(hold) == 3  # 30% of 10


def test_compare_profiles_detects_knob_delta(tmp_path: Path, meta) -> None:
    journal = tmp_path / "j.jsonl"
    _journal_fixture(
        journal,
        yes=meta.yes.token_id,
        no=meta.no.token_id,
        market=meta.condition_id,
    )
    baseline = StrategyProfile()
    # Stickier requotes + higher tox penalty should change cancel/quote counts
    # and/or inventory path vs defaults on a moving book.
    candidate = profile_from_overrides(
        baseline, {"reprice_ticks": 8, "c_tox": 6.0, "gamma": 1.2}
    )
    result = compare_profiles(
        journal, meta, baseline, candidate, tmp_path / "out"
    )
    d = result.as_dict()
    assert "delta" in d
    assert "n_quote" in d["delta"]
    assert "realized_spread_usdc" in d["delta"]
    assert "inventory_drift_abs_peak" in d["delta"]
    # Same tape → both sides produce quotes; deltas are numeric.
    assert result.baseline_replay["n_quote"] >= 1
    assert result.candidate_replay["n_quote"] >= 1
    assert isinstance(result.delta["n_quote"], (int, float))


def test_compare_holdout_window_runs(tmp_path: Path, meta) -> None:
    journal = tmp_path / "j.jsonl"
    _journal_fixture(
        journal,
        yes=meta.yes.token_id,
        no=meta.no.token_id,
        market=meta.condition_id,
    )
    result = compare_profiles(
        journal,
        meta,
        StrategyProfile(),
        profile_from_overrides(StrategyProfile(), {"gamma": 0.9}),
        tmp_path / "oos",
        holdout_frac=0.4,
        use_holdout=True,
    )
    assert result.window["mode"] == "holdout"
    assert result.window["n_events"] < result.window["n_events_full"]
    assert result.baseline_replay["events_read"] == result.window["n_events"]


def test_load_named_profile_newsom() -> None:
    from polymaker.replay.compare import load_named_profile

    p = load_named_profile("newsom-mm", config_dir="config")
    assert p.gamma == 0.6
    assert p.reward_size_mult == 1.5


def test_regime_journal_compare_named_profiles(tmp_path: Path, meta) -> None:
    from polymaker.replay.compare import load_named_profile
    from polymaker.replay.synth import write_regime_journal

    journal = tmp_path / "regime.jsonl"
    info = write_regime_journal(
        journal,
        yes_token=meta.yes.token_id,
        no_token=meta.no.token_id,
        market=meta.condition_id,
    )
    assert info["n_events"] >= 10
    baseline = StrategyProfile()
    candidate = load_named_profile("newsom-mm", config_dir="config")
    result = compare_profiles(
        journal, meta, baseline, candidate, tmp_path / "named"
    )
    assert result.baseline_replay["n_quote"] >= 1
    assert result.candidate_replay["n_quote"] >= 1
    assert "n_quote" in result.delta
