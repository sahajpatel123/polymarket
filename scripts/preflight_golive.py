#!/usr/bin/env python3
"""Go-live preflight: every check that can be made WITHOUT credentials.

Run this before pointing the engine at real money. It fails closed — any FAIL
means do not go live. Credential-dependent checks (wallet balance, API auth,
allowance) are deliberately out of scope; run ``polymaker doctor`` and
``polymaker livetest`` for those once keys are in place.

    uv run python scripts/preflight_golive.py --config-dir config
    uv run python scripts/preflight_golive.py --config-dir config --json
"""

from __future__ import annotations

import argparse
import inspect
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool | None, detail: str, fix: str = "",
            warn_only: bool = False) -> None:
        if ok is None:
            status = SKIP
        elif ok:
            status = PASS
        else:
            status = WARN if warn_only else FAIL
        self.checks.append(Check(name, status, detail, fix))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]


# ── risk / capital ───────────────────────────────────────────────────────


def check_risk(rep: Report, cfg: Any) -> None:
    r = cfg.risk.resolve_from_bankroll()
    bank = float(cfg.risk.bankroll_usdc or 0.0)
    rep.add("risk.bankroll_set", bank > 0,
            f"bankroll_usdc={bank}",
            "set risk.bankroll_usdc — all caps derive from it")
    if bank > 0:
        rep.add("risk.daily_loss_cap_sane",
                0 < r.daily_loss_kill_usdc <= 0.25 * bank,
                f"daily_loss_kill={r.daily_loss_kill_usdc} on bankroll {bank}",
                "keep the daily loss cap at <=25% of bankroll")
        rep.add("risk.per_market_cap_sane",
                0 < r.max_market_notional_usdc <= bank,
                f"max_market_notional={r.max_market_notional_usdc}",
                "per-market notional must not exceed the bankroll")
        rep.add("risk.total_exposure_sane",
                0 < r.max_total_exposure_usdc <= bank * 1.001,
                f"max_total_exposure={r.max_total_exposure_usdc}",
                "total exposure must not exceed the bankroll")
    rep.add("execution.post_only", bool(cfg.execution.post_only),
            f"post_only={cfg.execution.post_only}",
            "maker-only strategy must post_only, or it pays taker fees")
    rep.add("engine.heartbeat_on", bool(cfg.engine.heartbeat),
            f"heartbeat={cfg.engine.heartbeat}",
            "heartbeat is the exchange dead-man switch; required live")


# ── daily-loss stop behaviour ────────────────────────────────────────────


def check_kill_switch(rep: Report, tmp: Path) -> None:
    from polymaker.config import RiskConfig
    from polymaker.domain import Fill, Side
    from polymaker.risk.manager import RiskManager
    from polymaker.state.store import StateStore

    cfg = RiskConfig(max_total_exposure_usdc=5000, max_market_notional_usdc=800,
                     max_event_group_loss_usdc=1000, daily_loss_kill_usdc=100)
    db = tmp / "preflight_risk.db"
    store = StateStore(db)
    rm = RiskManager(cfg, store)
    rm.begin_day()
    tok = "tok"
    f = Fill(tok, Side.BUY, 0.50, 1000, "t1")
    store.apply_fill(f)
    rm.note_fill(f)
    rm.update_mark(tok, 0.50)
    before = rm.global_halt()[0]
    rm.update_mark(tok, 0.20)          # -300 vs -100 cap
    tripped = rm.global_halt()[0]
    rm.update_mark(tok, 0.50)          # marks recover
    latched = rm.global_halt()[0]
    store.close()

    store2 = StateStore(db)
    rm2 = RiskManager(cfg, store2)
    rm2.update_mark(tok, 0.50)
    rm2.begin_day()                    # simulate a process restart, same day
    survives = rm2.global_halt()[0]
    store2.close()

    rep.add("kill.not_tripped_when_flat", not before, "halt off at zero loss")
    rep.add("kill.trips_on_breach", tripped, "halt on -300 vs -100 cap",
            "daily loss cap is not firing")
    rep.add("kill.latches_through_recovery", latched,
            "stop held when marks recovered",
            "an unlatched stop releases on a favourable tick and re-arms buying")
    rep.add("kill.survives_restart", survives,
            "same-day restart resumed the breached budget",
            "a restart must not hand back a fresh daily allowance")


# ── exit path ────────────────────────────────────────────────────────────


def check_exit_path(rep: Report) -> None:
    from polymaker.domain import MarketMeta, Position, Regime, Side, TokenMeta
    from polymaker.marketdata.orderbook import BookView
    from polymaker.strategy.quoting import _maybe_exit, clamp_sell_exposure

    tick, dec, tok = 0.01, 2, "yes"
    meta = MarketMeta(
        condition_id="0xc", question="q", slug="s",
        tokens=(TokenMeta(tok, "Yes"), TokenMeta("no", "No")),
        tick_size=tick, neg_risk=False, min_order_size=5.0,
        rewards_min_size=10.0, rewards_max_spread=3.0, rewards_daily_rate=50.0,
        maker_fee_bps=0, taker_fee_bps=100, fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z", event_id="e")
    view = BookView(best_bid=0.19, best_bid_size=500, best_ask=0.20,
                    best_ask_size=500, second_bid=0.18, second_ask=0.21,
                    bid_depth=5000, ask_depth=5000)

    def exit_px(cost: float, fv: float, urgency: float) -> float | None:
        q: list[Any] = []
        _maybe_exit(q, tok, Position(tok, 200.0, cost), fv, 0.05, view, tick,
                    dec, urgency, meta, Regime.QUIET, stop_loss_pct=0.015)
        return q[0].price if q else None

    # reachable: an exit above the ask can never be hit
    px_flat = exit_px(0.19, 0.19, 0.0)
    rep.add("exit.reachable", px_flat is not None and px_flat <= view.best_ask,
            f"flat-market exit at {px_flat} vs ask {view.best_ask}",
            "exit priced above the ask can never fill; inventory is stranded")

    # post-only safety: a SELL at or below the bid would be rejected live
    worst = exit_px(0.30, 0.10, 1.0)      # deep loss, max urgency
    rep.add("exit.never_crosses_the_bid",
            worst is not None and worst > view.best_bid,
            f"most aggressive exit {worst} vs bid {view.best_bid}",
            "post_only rejects a sell at/below the bid — the exit would fail")

    # profit awareness
    px_small_loss = exit_px(0.195, 0.19, 0.0)
    rep.add("exit.holds_through_noise",
            px_small_loss is not None and px_small_loss > 0.195,
            f"exit {px_small_loss} vs cost 0.195 at low urgency",
            "exit dumps below cost on noise")

    # oversell guard
    from polymaker.domain import Quote
    clamped = clamp_sell_exposure(
        [Quote(tok, Side.SELL, 0.20, 100.0), Quote(tok, Side.SELL, 0.21, 100.0)],
        {tok: 50.0}, min_order_size=5.0)
    sold = sum(q.size for q in clamped if q.side is Side.SELL)
    rep.add("exit.never_oversells", sold <= 50.0 + 1e-9,
            f"offered {sold} against 50 held",
            "no OCO on the exchange: overlapping exits can flip long to short")


# ── paper/live separation ────────────────────────────────────────────────


def check_mode_separation(rep: Report) -> None:
    from polymaker.engine import Engine

    src = inspect.getsource(Engine._check_position_divergence)
    rep.add("mode.divergence_paper_exempt", "if self.paper:" in src,
            "paper mode skips on-chain position reconciliation",
            "paper positions get force-zeroed against an empty wallet")
    src2 = inspect.getsource(Engine._reconcile_loop)
    rep.add("mode.rest_reconcile_live_only", "if not self.paper:" in src2,
            "REST order/position reconcile is live-only",
            "empty REST snapshots delete paper orders the simulator still holds")
    rep.add("mode.live_divergence_still_active",
            "force_set_position" in src,
            "live mode still corrects to on-chain truth",
            "live safety must not be weakened by the paper exemption")


# ── model / governor ─────────────────────────────────────────────────────


def check_model(rep: Report, cfg: Any) -> None:
    from polymaker.strategy.fill_model import FillModel

    path = Path(cfg.paths.model_dir) / "fill_model.pkl"
    if not path.exists():
        rep.add("model.artifact_present", None, f"{path} missing",
                "train one (scripts/train_fill_model.py) or run without the ML gate")
        return
    try:
        model, store = FillModel.load_bundle(path)
    except Exception as exc:
        rep.add("model.loads", False, f"{path}: {exc}", "artifact is unreadable")
        return
    rep.add("model.loads", True, f"{path} loaded, trained={model.is_trained}")
    rep.add("model.no_silent_fallback", model.fallback_count == 0,
            f"fallback_count={model.fallback_count}",
            "a model that degraded to the heuristic must not gate quotes")
    gate = cfg.model
    rep.add("model.live_gate_configured",
            gate.min_live_validation_samples > 0 and gate.min_auc >= 0.5,
            f"min_samples={gate.min_live_validation_samples} "
            f"min_auc={gate.min_auc} min_corr={gate.min_markout_corr}",
            "the deployment gate must require real online evidence")
    online = store.online_arrays() if store is not None else None
    n_online = 0 if online is None else len(online[0])
    rep.add("model.online_samples_for_gate",
            n_online >= gate.min_live_validation_samples,
            f"{n_online}/{gate.min_live_validation_samples} online samples",
            "keep running paper until the gate has evidence; until then the ML "
            "gate stays in shadow and the tree/heuristic applies",
            warn_only=True)


def check_governor(rep: Report) -> None:
    from polymaker.engine import Engine

    src = inspect.getsource(Engine._resolve_fill_labels)
    rep.add("governor.not_driven_by_markout",
            "win_gov.record_outcome" not in src,
            "markout labels no longer throttle entry volume",
            "markout scores spread capture as a loss and would block entries")
    src2 = inspect.getsource(Engine._on_fill)
    rep.add("governor.driven_by_realized_pnl",
            "realized_pnl" in src2 and "record_outcome" in src2,
            "governor learns from realized round-trip PnL")


# ── provider wiring ──────────────────────────────────────────────────────


def check_llm(rep: Report) -> None:
    from polymaker.intelligence.agent import resolve_model
    from polymaker.intelligence.self_improve import (
        CHAT_COMPLETIONS_URL,
        REASONING_MODEL,
    )

    rep.add("llm.endpoint_matches_model",
            "deepseek" in CHAT_COMPLETIONS_URL and "deepseek" in REASONING_MODEL,
            f"{REASONING_MODEL} -> {CHAT_COMPLETIONS_URL}",
            "model/provider mismatch returns HTTP 400 on every call")
    try:
        resolve_model({})
        ok = True
        detail = "resolve_model() works on a clean environment"
    except Exception as exc:
        ok, detail = False, f"resolve_model() raises: {exc}"
    rep.add("llm.resolve_model_clean_env", ok, detail,
            "default model must satisfy the helper's own policy")
    src = inspect.getsource(
        __import__("polymaker.engine", fromlist=["Engine"]).Engine._review_loop)
    rep.add("llm.blocking_call_off_event_loop", "to_thread" in src,
            "daily review runs in a worker thread",
            "a 90s blocking urlopen on the loop stalls quoting and the heartbeat")


# ── operational ──────────────────────────────────────────────────────────


def check_ops(rep: Report, cfg: Any) -> None:
    db = Path(cfg.paths.db)
    rep.add("ops.db_parent_exists", db.parent.exists(),
            f"{db.parent}", "create the state db directory")
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            tables = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
            con.close()
            rep.add("ops.day_anchor_table", "day_anchor" in tables,
                    "day_anchor present" if "day_anchor" in tables
                    else "day_anchor missing (created on next start)",
                    warn_only=True)
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            n_live = con.execute(
                "select count(*) from fills where trade_id not like 'paper%'"
            ).fetchone()[0]
            con.close()
            rep.add("ops.state_db_is_paper_only", n_live == 0,
                    f"{n_live} non-paper fills in {db}",
                    "use a SEPARATE db path for live; paper history would "
                    "corrupt live PnL and the daily anchor", warn_only=True)
        except sqlite3.Error as exc:
            rep.add("ops.db_readable", False, str(exc))
    else:
        rep.add("ops.state_db_is_paper_only", None, f"{db} not created yet")
    rep.add("ops.alert_webhook_set",
            bool(getattr(cfg.secrets, "alert_webhook_url", "")),
            "ALERT_WEBHOOK_URL " + ("set" if getattr(
                cfg.secrets, "alert_webhook_url", "") else "unset"),
            "without it, kill-switch and crash alerts go nowhere",
            warn_only=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from polymaker.config import Config
    cfg = Config.load(args.config_dir)

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    rep = Report()
    for fn, a in (
        (check_risk, (cfg,)), (check_kill_switch, (tmp,)), (check_exit_path, ()),
        (check_mode_separation, ()), (check_model, (cfg,)), (check_governor, ()),
        (check_llm, ()), (check_ops, (cfg,)),
    ):
        try:
            fn(rep, *a)                       # type: ignore[operator]
        except Exception as exc:              # a broken check is itself a failure
            rep.add(f"{fn.__name__}.crashed", False, repr(exc))

    if args.json:
        print(json.dumps([c.__dict__ for c in rep.checks], indent=2))
    else:
        print(f"\n=== GO-LIVE PREFLIGHT — config-dir={args.config_dir} ===\n")
        icon = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}
        for c in rep.checks:
            print(f"  [{icon[c.status]}] {c.name}")
            print(f"         {c.detail}")
            if c.status in (FAIL, WARN) and c.fix:
                print(f"         -> {c.fix}")
        n = len(rep.checks)
        print(f"\n  {n - len(rep.failed) - len(rep.warned)}/{n} pass, "
              f"{len(rep.warned)} warn, {len(rep.failed)} FAIL")
        print("\n  Credentials are out of scope here — run `polymaker doctor` "
              "and `polymaker livetest` once keys are set.\n")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
