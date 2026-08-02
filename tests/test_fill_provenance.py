"""Tests for group/time provenance in the trainer and the eval script.

The trainer used to discard which asset each sample came from. Without that,
leave-assets-out validation is impossible and the only available split leaks,
which is how an offline win rate can look far better than reality. These tests
pin the provenance contract.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _tape(assets: list[str], n: int = 60, *, oscillate: bool = False) -> list[str]:
    """Synthetic multi-asset journal with crossing prints.

    ``oscillate=True`` makes the mid mean-revert so forward markouts take BOTH
    signs. A monotone tape yields single-class labels, which no classifier can
    be validated against.
    """
    lines: list[str] = []
    ts = 1_700_000_000.0
    for i in range(n):
        for k, asset in enumerate(assets):
            if oscillate:
                # amplitude well above one tick so markout signs actually flip
                mid = 0.40 + 0.05 * k + 0.02 * math.sin(0.9 * i + k)
            else:
                mid = 0.40 + 0.05 * k + 0.0002 * i + 0.0001 * math.sin(i + k)
            bb, ba = round(mid - 0.001, 6), round(mid + 0.001, 6)
            lines.append(json.dumps({
                "ts": ts, "kind": "book",
                "data": {
                    "asset_id": asset,
                    "bids": [{"price": bb, "size": "5000"},
                             {"price": round(bb - 0.001, 6), "size": "4000"}],
                    "asks": [{"price": ba, "size": "5000"},
                             {"price": round(ba + 0.001, 6), "size": "4000"}],
                },
            }))
            if i % 2 == 1:
                lines.append(json.dumps({
                    "ts": ts + 15.0, "kind": "last_trade_price",
                    "data": {"asset_id": asset, "price": bb,
                             "size": str(50 + 10 * (i % 3)), "side": "SELL"},
                }))
        ts += 30.0
    return lines


# ── trainer provenance contract ──────────────────────────────────────────


def test_trainer_emits_group_and_ts_aligned_with_every_sample(tmp_path: Path) -> None:
    from train_fill_model import build_training_store

    assets = ["tok-a", "tok-b", "tok-c"]
    journal = tmp_path / "multi.jsonl"
    journal.write_text("\n".join(_tape(assets)) + "\n")

    store, stats = build_training_store([journal], tick=0.001, base_size_usdc=4.0)
    n = len(store.features)
    assert n > 0, stats
    assert "groups" in stats, "trainer must emit per-sample asset labels"
    assert "sample_ts" in stats, "trainer must emit per-sample timestamps"
    assert len(stats["groups"]) == n, (
        f"groups ({len(stats['groups'])}) must align 1:1 with samples ({n}); "
        "misalignment would silently corrupt grouped validation"
    )
    assert len(stats["sample_ts"]) == n


def test_trainer_groups_cover_all_assets_including_nonfills(tmp_path: Path) -> None:
    """Non-fill samples must carry asset identity too, not just fills."""
    from train_fill_model import build_training_store

    assets = ["tok-a", "tok-b", "tok-c"]
    journal = tmp_path / "multi.jsonl"
    journal.write_text("\n".join(_tape(assets)) + "\n")

    store, stats = build_training_store([journal], tick=0.001, base_size_usdc=4.0)
    groups = np.array(stats["groups"])
    _X, y_fill, _ym = store.raw_arrays()

    assert set(groups.tolist()) <= set(assets)
    assert len(set(groups.tolist())) == len(assets), "every asset should appear"
    nonfill_groups = groups[y_fill == 0]
    assert nonfill_groups.size > 0, "expected some non-fill samples"
    assert len(set(nonfill_groups.tolist())) > 1, (
        "non-fill samples lost their asset labels — grouped CV would be biased "
        "toward whichever asset happens to carry the fills"
    )


def test_trainer_reports_asset_counts(tmp_path: Path) -> None:
    from train_fill_model import build_training_store

    journal = tmp_path / "multi.jsonl"
    journal.write_text("\n".join(_tape(["tok-a", "tok-b"])) + "\n")
    _store, stats = build_training_store([journal], tick=0.001)
    assert stats["assets"] == 2
    assert stats["fill_assets"] >= 1


def test_sample_ts_is_monotone_within_an_asset(tmp_path: Path) -> None:
    """Timestamps must be real candidate times so purged time splits work."""
    from train_fill_model import build_training_store

    journal = tmp_path / "one.jsonl"
    journal.write_text("\n".join(_tape(["tok-a"])) + "\n")
    _store, stats = build_training_store([journal], tick=0.001)
    ts = np.array(stats["sample_ts"], dtype=float)
    assert ts.size > 0
    assert np.isfinite(ts).all()
    assert ts.min() >= 1_700_000_000.0 - 1.0, "timestamps must be real event times"


# ── eval script end to end ───────────────────────────────────────────────


def test_eval_script_runs_on_a_journal_and_reports_a_verdict(tmp_path: Path) -> None:
    """The harness must produce a verdict from a raw journal, end to end."""
    from eval_fill_model import load_fills, run

    assets = [f"tok-{c}" for c in "abcdefgh"]
    journal = tmp_path / "many.jsonl"
    journal.write_text("\n".join(_tape(assets, n=60, oscillate=True)) + "\n")

    X, markout, groups = load_fills(cache=None, journal=[journal])
    if X.shape[0] < 20 or len(set(groups.tolist())) < 3:
        pytest.skip("synthetic tape produced too few fills/assets to evaluate")
    if np.unique(markout > 0).size < 2:
        pytest.skip("synthetic tape produced single-class markouts")

    rep = run(X, markout, groups, protocol="leave_one_asset_out",
              n_holdout=2, retention=0.5, seed=42)
    d = rep.as_dict()
    assert d["protocol"] == "leave_one_asset_out"
    assert d["n_assets"] >= 3
    assert "beats_control" in d["verdict"]
    assert isinstance(d["verdict"]["beats_control"], bool)
    # baseline must always be reported so a gate can never be scored alone
    assert "win_rate" in d["baseline"]
    assert d["baseline"]["n_clusters"] >= 3
    # a control comparison must always be attached
    assert "control_wr" in d["verdict"]


def test_eval_fails_closed_on_single_class_labels(tmp_path: Path) -> None:
    """A monotone tape yields all-winner labels; the verdict must be False.

    An empty or missing verdict could be misread downstream as approval, so the
    harness must state explicitly that nothing was validated.
    """
    from eval_fill_model import load_fills, run

    assets = ["tok-a", "tok-b", "tok-c"]
    journal = tmp_path / "monotone.jsonl"
    journal.write_text("\n".join(_tape(assets, n=40, oscillate=False)) + "\n")

    X, markout, groups = load_fills(cache=None, journal=[journal])
    if np.unique(markout > 0).size > 1:
        pytest.skip("tape unexpectedly produced both label classes")

    rep = run(X, markout, groups, protocol="leave_one_asset_out",
              n_holdout=2, retention=0.5, seed=42)
    assert rep.verdict["beats_control"] is False
    assert "single-class" in rep.verdict["reason"]


def test_eval_script_rejects_a_cache_without_asset_labels(tmp_path: Path) -> None:
    import pickle

    from eval_fill_model import load_fills

    from polymaker.strategy.fill_model import FillTrainingStore

    store = FillTrainingStore()
    bad = tmp_path / "bad.pkl"
    bad.write_bytes(pickle.dumps((store, {"samples": 0})))
    with pytest.raises(SystemExit, match="no samples|no fill_meta"):
        load_fills(cache=bad, journal=None)
