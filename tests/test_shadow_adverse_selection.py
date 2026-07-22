"""Tests for fill-independent shadow adverse-selection metrics."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.metrics.shadow_as import analyze_shadow_as


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_shadow_as_buy_adverse_and_cross(tmp_path: Path) -> None:
    cid = "0xabc"
    t0 = 1_700_000_000.0
    rows = [
        {
            "ts": t0,
            "event": "mark",
            "condition_id": cid,
            "fv": 0.50,
            "regime": "QUIET",
            "inventory_net": 0.0,
        },
        {
            "ts": t0,
            "event": "quote",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.49,
            "size": 10.0,
            "order_id": "p1",
            "mid": 0.50,
            "in_reward_band": True,
        },
        # mid drops through our bid → crossed + adverse for BUY
        {
            "ts": t0 + 10,
            "event": "mark",
            "condition_id": cid,
            "fv": 0.48,
            "regime": "TRENDING",
            "inventory_net": 0.0,
        },
        {
            "ts": t0 + 35,
            "event": "mark",
            "condition_id": cid,
            "fv": 0.47,
            "regime": "TRENDING",
            "inventory_net": 0.0,
        },
        {
            "ts": t0 + 40,
            "event": "cancel",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.49,
            "size": 10.0,
            "order_id": "p1",
        },
    ]
    path = tmp_path / "m.jsonl"
    _write(path, rows)
    rep = analyze_shadow_as(path)
    assert rep.n_quote_lifetimes == 1
    assert rep.n_crossed == 1
    assert rep.crossed_frac == 1.0
    assert abs(rep.mean_edge_at_place - 0.01) < 1e-9  # mid 0.50 - bid 0.49
    assert rep.markout_n["30s"] == 1
    # BUY: mid 0.50 → 0.47 = -0.03 adverse
    assert abs(rep.markout_mean["30s"] - (-0.03)) < 1e-9
    assert "QUIET" in rep.by_regime_at_place


def test_shadow_as_replace_closes_prior_life(tmp_path: Path) -> None:
    cid = "0xabc"
    t0 = 1_700_000_000.0
    rows = [
        {"ts": t0, "event": "mark", "condition_id": cid, "fv": 0.50, "regime": "QUIET"},
        {
            "ts": t0,
            "event": "quote",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.49,
            "size": 10.0,
            "order_id": "p1",
            "mid": 0.50,
        },
        {
            "ts": t0 + 5,
            "event": "quote",
            "condition_id": cid,
            "token_id": "yes",
            "side": "BUY",
            "price": 0.485,
            "size": 10.0,
            "order_id": "p2",
            "mid": 0.50,
        },
        {"ts": t0 + 10, "event": "mark", "condition_id": cid, "fv": 0.51, "regime": "QUIET"},
    ]
    path = tmp_path / "m.jsonl"
    _write(path, rows)
    rep = analyze_shadow_as(path)
    # p1 closed at replace; p2 closed at EOF
    assert rep.n_quote_lifetimes == 2


def test_shadow_as_uses_fv_yes_when_present(tmp_path: Path) -> None:
    """Explicit fv_yes beats nearest-mark heuristic for YES-space remap."""
    cid = "0xabc"
    t0 = 1_700_000_000.0
    rows = [
        # Misleading mark far from quote — fv_yes on the quote must win.
        {"ts": t0, "event": "mark", "condition_id": cid, "fv": 0.90, "regime": "QUIET"},
        {
            "ts": t0,
            "event": "quote",
            "condition_id": cid,
            "token_id": "no",
            "side": "BUY",
            "price": 0.79,
            "size": 10.0,
            "order_id": "n1",
            "mid": 0.80,
            "fv_yes": 0.20,
        },
        {"ts": t0 + 35, "event": "mark", "condition_id": cid, "fv": 0.23, "regime": "QUIET"},
        {
            "ts": t0 + 40,
            "event": "cancel",
            "condition_id": cid,
            "token_id": "no",
            "side": "BUY",
            "price": 0.79,
            "size": 10.0,
            "order_id": "n1",
        },
    ]
    path = tmp_path / "m.jsonl"
    _write(path, rows)
    rep = analyze_shadow_as(path)
    assert rep.n_quote_lifetimes == 1
    assert abs(rep.mean_edge_at_place - 0.01) < 1e-9
    assert abs(rep.markout_mean["30s"] - (-0.03)) < 1e-9


def test_shadow_as_no_token_remapped_to_yes_space(tmp_path: Path) -> None:
    """NO-token mid~0.8 must not be compared raw against YES mark fv~0.2."""
    cid = "0xabc"
    t0 = 1_700_000_000.0
    rows = [
        {"ts": t0, "event": "mark", "condition_id": cid, "fv": 0.20, "regime": "QUIET"},
        {
            "ts": t0,
            "event": "quote",
            "condition_id": cid,
            "token_id": "no",
            "side": "BUY",
            "price": 0.79,
            "size": 10.0,
            "order_id": "n1",
            "mid": 0.80,  # NO mid; YES equiv = 0.20
        },
        # YES fv rises → NO falls → BUY NO is adverse → negative signed
        {"ts": t0 + 35, "event": "mark", "condition_id": cid, "fv": 0.23, "regime": "QUIET"},
        {
            "ts": t0 + 40,
            "event": "cancel",
            "condition_id": cid,
            "token_id": "no",
            "side": "BUY",
            "price": 0.79,
            "size": 10.0,
            "order_id": "n1",
        },
    ]
    path = tmp_path / "m.jsonl"
    _write(path, rows)
    rep = analyze_shadow_as(path)
    assert rep.n_quote_lifetimes == 1
    # BUY NO @ 0.79 → yes_side=SELL, yes_price=0.21, yes_mid=0.20
    # edge for SELL = price - mid = 0.21 - 0.20 = 0.01
    assert abs(rep.mean_edge_at_place - 0.01) < 1e-9
    # SELL YES: rise in fv is adverse → signed = -(0.23-0.20) = -0.03
    assert abs(rep.markout_mean["30s"] - (-0.03)) < 1e-9
