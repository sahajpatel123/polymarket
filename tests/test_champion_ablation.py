"""Champion–challenger + intelligence ablations on a fixed journal fixture."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.benchmark import evaluate_benchmark, evaluate_financial_claim, ValidityConfig
from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics.analyze import analyze
from polymaker.replay import run_replay


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xabl",
        question="ablation",
        slug="abl",
        tokens=(TokenMeta("yes-a", "Yes"), TokenMeta("no-a", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=5.0,
        rewards_daily_rate=80.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-01-01T00:00:00Z",
        event_id="abl",
    )


def _journal(path: Path) -> None:
    t0 = 1_700_000_000.0
    yes, no = "yes-a", "no-a"
    rows = []
    for i, tok in enumerate((yes, no)):
        rows.append({
            "ts": t0 + i * 0.01,
            "kind": "book",
            "data": {
                "market": "0xabl",
                "asset_id": tok,
                "bids": [{"price": "0.47", "size": "500"}, {"price": "0.48", "size": "500"}],
                "asks": [{"price": "0.52", "size": "500"}, {"price": "0.53", "size": "500"}],
                "timestamp": str(int((t0 + i * 0.01) * 1000)),
                "tick_size": "0.01",
            },
        })
    for i in range(80):
        ts = t0 + 1 + i * 0.5
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": "0xabl",
                "asset_id": yes,
                "bids": [{"price": "0.48", "size": "400"}],
                "asks": [{"price": "0.52", "size": "400"}],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.01",
            },
        })
        if i % 2 == 0:
            rows.append({
                "ts": ts + 0.1,
                "kind": "last_trade_price",
                "data": {
                    "market": "0xabl",
                    "asset_id": yes,
                    "price": "0.48",
                    "size": "30",
                    "side": "SELL",
                    "timestamp": str(int((ts + 0.1) * 1000)),
                },
            })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_champion_and_intel_ablations(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    _journal(journal)
    meta = _meta()
    configs = [
        ("baseline_naive", StrategyProfile(), "off"),
        ("intel_off", StrategyProfile(use_intelligence=False), "off"),
        ("intel_gate_only", StrategyProfile(use_intelligence=True, intelligence_mode="gate_only"), "gate_only"),
        ("intel_full", StrategyProfile(use_intelligence=True, intelligence_mode="full"), "full"),
    ]
    rows_out = []
    for name, profile, mode_label in configs:
        # Force mode into profile for gate_only/full
        if mode_label == "gate_only":
            profile = StrategyProfile(use_intelligence=True, intelligence_mode="gate_only")
        elif mode_label == "full":
            profile = StrategyProfile(use_intelligence=True, intelligence_mode="full")
        mpath = tmp_path / f"{name}.jsonl"
        rr = run_replay(
            journal, meta, profile, mpath,
            fill_mode="conservative",
            strict_sync=True,
        )
        rep = analyze(mpath)
        v = evaluate_benchmark(
            n_quote=rr.n_quote,
            n_fill=rr.n_fill,
            n_mark=rr.n_mark,
            n_markets=1,
            runtime_s=100.0,
            n_trade_prints=40,
            state_divergence_events=rr.state_divergence_events,
            fills_after_cancel=rr.fills_after_cancel,
            overfills=rr.overfills,
            cfg=ValidityConfig(
                min_quotes=1, min_fills=0, min_marks=1,
                min_runtime_s=0, min_trade_prints=0,
                require_actionable_quotes=False,
            ),
        )
        fin = evaluate_financial_claim(
            validity=v,
            honest_pnl=rep.honest_pnl,
            fill_mode="conservative",
        )
        rows_out.append({
            "name": name,
            "champion": name == "baseline_naive",
            "intel_mode": mode_label,
            "n_quote": rr.n_quote,
            "n_fill": rr.n_fill,
            "validity": v.status.value,
            "financial": fin.status.value,
            "honest_without_rewards": rep.honest_pnl.get("pnl_without_rewards_usdc"),
            "divergence": rr.state_divergence_events,
        })
    assert any(r["champion"] for r in rows_out)
    modes = {r["intel_mode"] for r in rows_out}
    assert "off" in modes and "full" in modes and "gate_only" in modes
    for r in rows_out:
        assert r["divergence"] == 0
        assert r["validity"]  # non-empty status
    # Persist for offline evidence consumers
    (tmp_path / "ablation_table.json").write_text(json.dumps(rows_out, indent=2))
