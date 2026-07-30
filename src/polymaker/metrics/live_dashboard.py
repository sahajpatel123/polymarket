"""Live localhost operator dashboard — opens when the bot starts.

Serves a single-page multi-layout UI + JSON snapshot API on 127.0.0.1.
No heavy framework: stdlib HTTP server in a daemon thread, auto-refresh
in the browser. Designed for glanceable ops (Pulse / Book / Risk / Tape).

Operator surface
----------------
- Layouts: Pulse (1) · Book (2) · Risk (3) · Tape (4); Esc → Pulse; ``i`` inventory filter
- Deep links: ``http://127.0.0.1:8765/#risk``
- ``GET /api/snapshot`` — full JSON; ``GET /healthz`` — probe (503 if CRITICAL)
- Engine writes real URL to ``logs/dashboard.url`` (port may bump if busy)
- Bind is loopback-only unless ``POLYMAKER_DASHBOARD_ALLOW_REMOTE=1``
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("polymaker.metrics.live_dashboard")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SNAPSHOT_STALE_S = 15.0


def _require_loopback_host(host: str) -> str:
    """Keep the operator UI on loopback unless explicitly overridden.

    Set ``POLYMAKER_DASHBOARD_ALLOW_REMOTE=1`` to bind a non-loopback host
    (not recommended — snapshot includes inventory / PnL).
    """
    h = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    if h in _LOOPBACK_HOSTS:
        return h
    if os.environ.get("POLYMAKER_DASHBOARD_ALLOW_REMOTE", "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
    }:
        log.warning("dashboard_remote_bind_allowed", host=h)
        return h
    raise ValueError(
        f"dashboard host {h!r} is not loopback; use 127.0.0.1 "
        "or set POLYMAKER_DASHBOARD_ALLOW_REMOTE=1"
    )


@dataclass
class DashboardSnapshot:
    """Everything the UI needs in one payload."""

    ts: float
    generated_at: str
    mode: str  # PAPER | LIVE
    health: str  # OK | ACTIVE | WARN | CRITICAL | NO_DATA
    health_detail: str
    capital_usdc: float
    equity: float | None
    daily_pnl: float | None
    net_cash: float | None
    inventory_value: float | None
    open_orders: int
    n_markets: int
    n_quote: int
    n_fill: int
    n_cancel: int
    realized_spread_usdc: float
    inventory_peak: float
    markout: dict[str, float] = field(default_factory=dict)
    markets: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    risk: dict[str, Any] = field(default_factory=dict)
    outage: dict[str, Any] = field(default_factory=dict)
    insights: list[str] = field(default_factory=list)
    url_hint: str = ""
    # Live link health: market_ws / user_ws / heartbeat / outage
    links: dict[str, Any] = field(default_factory=dict)
    metrics_age_s: float | None = None
    version: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _health_from(
    *,
    n_quote: int,
    n_fill: int,
    n_bad: int,
    outage_open: bool,
    daily_pnl: float | None,
    kill_usdc: float | None,
) -> tuple[str, str]:
    if outage_open:
        return "CRITICAL", "Upstream/outage open — quoting may be stalled"
    if n_bad > 0:
        return "WARN", "Metrics log has parse errors"
    if kill_usdc is not None and daily_pnl is not None and daily_pnl <= -0.7 * kill_usdc:
        return "WARN", f"Daily PnL approaching kill ({daily_pnl:+.2f} / -{kill_usdc:.0f})"
    if n_quote == 0 and n_fill == 0:
        return "NO_DATA", "No quotes or fills yet — waiting for tape"
    if n_fill > 0:
        return "ACTIVE", "Fills flowing — watch inventory & markouts"
    return "OK", "Quoting quietly — rewards posture"


def _read_outage(log_dir: Path) -> dict[str, Any]:
    path = log_dir / "outage_status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


_DB_CACHE: dict[str, tuple[float, float, int, Any]] = {}
_DB_CACHE_TTL_S = 1.5
_CACHE_MAX_KEYS = 32


def _cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
    cache[key] = value
    while len(cache) > _CACHE_MAX_KEYS:
        cache.pop(next(iter(cache)), None)


def _db_cache_get(path: Path, kind: str) -> Any | None:
    if not path.exists():
        return None
    try:
        st = path.stat()
        key = f"{kind}:{path.resolve()}"
        hit = _DB_CACHE.get(key)
        if hit is None:
            return None
        cached_at, mtime, size, payload = hit
        if (
            mtime == st.st_mtime
            and size == st.st_size
            and (time.time() - cached_at) < _DB_CACHE_TTL_S
        ):
            return payload
    except OSError:
        return None
    return None


def _db_cache_set(path: Path, kind: str, payload: Any) -> None:
    try:
        st = path.stat()
        key = f"{kind}:{path.resolve()}"
        _cache_put(_DB_CACHE, key, (time.time(), float(st.st_mtime), int(st.st_size), payload))
    except OSError:
        pass


def _read_pnl(db_path: Path) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "equity": None,
        "daily_pnl": None,
        "net_cash": None,
        "inventory_value": None,
    }
    if not db_path.exists():
        return out
    cached = _db_cache_get(db_path, "pnl")
    if isinstance(cached, dict):
        return cached  # type: ignore[return-value]
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT equity, daily_pnl, net_cash, inventory_value FROM pnl_snapshots "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is not None:
            out["equity"] = float(row["equity"]) if row["equity"] is not None else None
            out["daily_pnl"] = float(row["daily_pnl"]) if row["daily_pnl"] is not None else None
            out["net_cash"] = float(row["net_cash"]) if row["net_cash"] is not None else None
            out["inventory_value"] = (
                float(row["inventory_value"]) if row["inventory_value"] is not None else None
            )
        _db_cache_set(db_path, "pnl", out)
    except Exception:
        pass
    return out


def _read_state_store(db_path: Path) -> tuple[int, list[dict[str, Any]]]:
    if not db_path.exists():
        return 0, []
    cached = _db_cache_get(db_path, "state")
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached  # type: ignore[return-value]
    try:
        from polymaker.state.store import StateStore

        store = StateStore(db_path)
        snap = store.snapshot()
        open_orders = int(snap.get("open_orders") or 0)
        positions = []
        for tok, p in (snap.get("positions") or {}).items():
            if abs(float(p.get("size") or 0)) < 1e-9:
                continue
            positions.append(
                {
                    "token": str(tok)[:20] + "…",
                    "size": round(float(p.get("size") or 0), 4),
                    "avg_price": round(float(p.get("avg_price") or 0), 4),
                }
            )
        store.close()
        result = (open_orders, positions)
        _db_cache_set(db_path, "state", result)
        return result
    except Exception:
        return 0, []


_METRICS_CACHE: dict[str, tuple[float, float, int, dict[str, Any]]] = {}
_METRICS_CACHE_TTL_S = 2.5


def _metrics_bits(metrics_log: Path, *, force: bool = False) -> dict[str, Any]:
    empty = {
        "n_quote": 0,
        "n_fill": 0,
        "n_cancel": 0,
        "n_bad": 0,
        "realized_spread_usdc": 0.0,
        "inventory_peak": 0.0,
        "markout": {},
        "markets": [],
        "reward_accrual": {},
        "inventory_net": {},
    }
    if not metrics_log.exists():
        return empty
    try:
        st = metrics_log.stat()
        if st.st_size == 0:
            return empty
        mtime = float(st.st_mtime)
        size = int(st.st_size)
        key = str(metrics_log.resolve())
    except OSError:
        return empty
    now = time.time()
    if not force:
        hit = _METRICS_CACHE.get(key)
        if hit is not None:
            cached_at, cached_mtime, cached_size, payload = hit
            if (
                cached_mtime == mtime
                and cached_size == size
                and (now - cached_at) < _METRICS_CACHE_TTL_S
            ):
                return payload
    try:
        from polymaker.metrics.analyze import analyze

        rep = analyze(metrics_log)
        payload = {
            "n_quote": rep.n_quote,
            "n_fill": rep.n_fill,
            "n_cancel": rep.n_cancel,
            "n_bad": rep.n_bad,
            "realized_spread_usdc": round(rep.realized_spread_usdc, 4),
            "inventory_peak": round(rep.inventory_drift_abs_peak, 2),
            "markout": {k: round(v, 6) for k, v in rep.markout.items()},
            "markets": sorted(rep.markets),
            "reward_accrual": {k: round(v, 4) for k, v in rep.reward_accrual_usdc.items()},
            "inventory_net": {k: round(v, 4) for k, v in rep.inventory_net_end.items()},
        }
        _cache_put(_METRICS_CACHE, key, (now, mtime, size, payload))
        return payload
    except Exception:
        return empty


def build_insights(snap: DashboardSnapshot) -> list[str]:
    """Short, operator-facing sentences — the 'smart' layer.

    Critical tips (halt / blind links / toxicity) are kept ahead of soft notes
    so the 4-slot budget is not wasted on paper chatter during an incident.
    """
    critical: list[str] = []
    soft: list[str] = []
    if snap.risk.get("global_halt"):
        critical.append(
            f"Global risk halt — {snap.risk.get('halt_reason') or 'check kill / error rate'}."
        )
    links = snap.links or {}
    if links.get("market_ws") == "down":
        critical.append("Market WS disconnected — books stale; quoting will blind-halt.")
    if links.get("user_ws") == "down":
        critical.append("User WS down — fills may be missed until reconnect + reconcile.")
    if links.get("heartbeat") == "down":
        critical.append("Heartbeat failing — exchange may auto-cancel resting orders.")
    if snap.outage.get("outage_open") or links.get("outage") == "open":
        critical.append("Outage flag open — check connectivity / collector before trusting tape.")
    err = snap.risk.get("order_error_rate")
    attempts = int(snap.risk.get("order_attempts") or 0)
    if isinstance(err, (int, float)) and err >= 0.15 and attempts >= 20:
        critical.append(
            f"Order error rate {err:.0%} over {attempts} attempts — posting may be failing."
        )
    if snap.metrics_age_s is not None and snap.metrics_age_s > 120 and snap.risk.get("running"):
        critical.append(
            f"Metrics log quiet for {int(snap.metrics_age_s)}s — engine may be hung or not emitting."
        )
    mo30 = snap.markout.get("30s")
    if isinstance(mo30, (int, float)) and mo30 < -0.005 and snap.n_fill >= 5:
        critical.append(
            f"30s markout adverse ({mo30:+.4f}) — fills may be toxic; check size / thin books."
        )
    if snap.health == "CRITICAL" and not snap.risk.get("global_halt") and not links:
        critical.append("Fix outage before trusting quotes: check connectivity / collector.")

    frac = snap.risk.get("exposure_frac")
    if isinstance(frac, (int, float)) and frac >= 0.7:
        soft.append(f"Total exposure at {frac:.0%} of cap — size taper may already be on.")
    if snap.mode == "PAPER" and snap.n_fill == 0 and snap.n_quote > 50:
        soft.append("Paper is quoting but has no fills — reward farming posture, not PnL proof.")
    if snap.capital_usdc <= 0 and snap.risk.get("running"):
        soft.append("Capital is 0 — sizing/policy may be unloaded; check capital file / bankroll.")
    if snap.daily_pnl is not None and snap.daily_pnl < 0 and snap.n_fill > 0:
        soft.append("Negative day with fills — check markouts on Risk/Tape; size may be too large.")
    if snap.inventory_peak > 100:
        soft.append("Inventory peak is elevated — prefer exits / REDUCE_ONLY over adding.")
    if (
        snap.n_quote > 100
        and snap.n_cancel > 0
        and snap.n_quote > 0
        and (snap.n_cancel / snap.n_quote) > 0.85
    ):
        soft.append("Cancel/quote is very high — churn may be burning rate limit / missed rewards.")
    if snap.open_orders == 0 and snap.n_markets > 0 and snap.health in {"OK", "ACTIVE"}:
        soft.append("No open orders despite markets — regime may be HALTED/EVENT or WS blind.")
    halted_n = int(snap.risk.get("halted_markets") or 0)
    if halted_n > 0 and snap.n_markets > 0 and halted_n >= max(1, snap.n_markets // 2):
        soft.append(
            f"{halted_n}/{snap.n_markets} markets halted — check Gamma closed/not-accepting."
        )

    tips = critical + soft
    if not tips:
        tips.append(
            "Steady state. Watch Pulse for health color; switch to Book for per-market drift."
        )
    return tips[:4]


def build_snapshot_from_paths(
    *,
    db_path: str | Path,
    log_dir: str | Path,
    metrics_log: str | Path,
    paper: bool,
    capital_usdc: float = 0.0,
    kill_usdc: float | None = None,
    live_markets: list[dict[str, Any]] | None = None,
    risk_extra: dict[str, Any] | None = None,
    metrics_bits: dict[str, Any] | None = None,
) -> DashboardSnapshot:
    db = Path(db_path)
    logs = Path(log_dir)
    mlog = Path(metrics_log)
    pnl = _read_pnl(db)
    open_orders, positions = _read_state_store(db)
    bits = metrics_bits if metrics_bits is not None else _metrics_bits(mlog)
    outage = _read_outage(logs)
    outage_open = bool(outage.get("outage_open"))

    markets: list[dict[str, Any]] = []
    if live_markets:
        markets = live_markets
    else:
        inv = bits.get("inventory_net") or {}
        rewards = bits.get("reward_accrual") or {}
        for cid in bits.get("markets") or []:
            markets.append(
                {
                    "id": cid[:16] + ("…" if len(cid) > 16 else ""),
                    "condition_id": cid,
                    "regime": "—",
                    "inventory_net": inv.get(cid, 0.0),
                    "reward_accrual": rewards.get(cid, 0.0),
                    "question": "",
                }
            )

    health, detail = _health_from(
        n_quote=int(bits["n_quote"]),
        n_fill=int(bits["n_fill"]),
        n_bad=int(bits["n_bad"]),
        outage_open=outage_open,
        daily_pnl=pnl["daily_pnl"],
        kill_usdc=kill_usdc,
    )
    snap = DashboardSnapshot(
        ts=time.time(),
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        mode="PAPER" if paper else "LIVE",
        health=health,
        health_detail=detail,
        capital_usdc=float(capital_usdc or 0.0),
        equity=pnl["equity"],
        daily_pnl=pnl["daily_pnl"],
        net_cash=pnl["net_cash"],
        inventory_value=pnl["inventory_value"],
        open_orders=open_orders,
        n_markets=len(markets),
        n_quote=int(bits["n_quote"]),
        n_fill=int(bits["n_fill"]),
        n_cancel=int(bits["n_cancel"]),
        realized_spread_usdc=float(bits["realized_spread_usdc"]),
        inventory_peak=float(bits["inventory_peak"]),
        markout=dict(bits.get("markout") or {}),
        markets=markets,
        positions=positions,
        risk={
            "daily_loss_kill_usdc": kill_usdc,
            **(risk_extra or {}),
        },
        outage=outage,
        insights=[],
    )
    with contextlib.suppress(Exception):
        from polymaker import __version__

        snap.version = str(__version__)
    if (risk_extra or {}).get("global_halt"):
        snap.health = "CRITICAL"
        snap.health_detail = f"Risk halt: {(risk_extra or {}).get('halt_reason') or 'global_halt'}"
    with contextlib.suppress(Exception):
        if mlog.exists():
            snap.metrics_age_s = round(time.time() - mlog.stat().st_mtime, 1)
    snap.insights = build_insights(snap)
    return snap


def build_snapshot_from_engine(engine: Any) -> DashboardSnapshot:
    """Live snapshot using the running Engine (preferred when bot is up)."""
    cfg = engine.cfg
    paper = bool(engine.paper)
    metrics_name = "metrics-paper.jsonl" if paper else "metrics-live.jsonl"
    metrics_log = Path(cfg.paths.log_dir) / metrics_name
    capital = float(getattr(engine, "_effective_capital", 0) or 0)
    if capital <= 0:
        try:
            from polymaker.intelligence.policy import load_capital_usdc

            capital = float(load_capital_usdc())
        except Exception:
            capital = 0.0

    live_markets: list[dict[str, Any]] = []
    metrics_bits = _metrics_bits(metrics_log)
    rewards = metrics_bits.get("reward_accrual") or {}
    now = time.time()
    last_regimes = getattr(engine, "_last_regime", {}) or {}
    for cid, meta in getattr(engine, "metas", {}).items():
        cooloff_s = 0.0
        rm = getattr(engine, "regime_m", {}).get(cid)
        if cid in getattr(engine, "_halted", set()):
            regime = "HALTED"
        elif cid in getattr(engine, "_llm_paused", set()):
            regime = "PAUSED"
        elif rm is not None and getattr(rm, "in_cooloff", False):
            regime = "EVENT"
            with contextlib.suppress(Exception):
                cooloff_s = float(rm.cooloff_remaining(now))
        else:
            # Prefer last requote decision (QUIET / TRENDING / …) over a
            # static QUIET default — Book was lying when markets were live.
            regime = str(last_regimes.get(cid) or "QUIET")
        pos_yes = engine.state.position(meta.yes.token_id).size
        pos_no = engine.state.position(meta.no.token_id).size
        live_markets.append(
            {
                "id": (meta.slug or cid)[:28],
                "condition_id": cid,
                "regime": regime,
                "cooloff_s": round(cooloff_s, 1),
                "inventory_net": round(float(pos_yes - pos_no), 4),
                "reward_accrual": float(rewards.get(cid, 0.0) or 0.0),
                "question": (meta.question or "")[:80],
                "tick": meta.tick_size,
                "rewards_min_size": meta.rewards_min_size,
            }
        )
    # Book: surface problems first (HALTED/EVENT/PAUSED), then |inventory|.
    _sev = {"HALTED": 0, "PAUSED": 1, "EVENT": 2, "REDUCE_ONLY": 3, "TRENDING": 4}
    live_markets.sort(
        key=lambda m: (
            _sev.get(str(m.get("regime") or ""), 5),
            -abs(float(m.get("inventory_net") or 0)),
        )
    )

    risk_extra = {
        "halted_markets": len(getattr(engine, "_halted", set())),
        "llm_paused": len(getattr(engine, "_llm_paused", set())),
        "running": bool(getattr(engine, "_running", False)),
    }
    kill = float(getattr(cfg.risk, "daily_loss_kill_usdc", 0) or 0) or None

    # Prefer live RiskManager marks over last SQLite snapshot when available.
    live_equity = None
    live_pnl = None
    halt_reason = ""
    risk = getattr(engine, "risk", None)
    if risk is not None:
        with contextlib.suppress(Exception):
            live_equity = float(risk.equity())
            live_pnl = float(getattr(risk, "daily_pnl", 0.0))
            halted, why = risk.global_halt()
            risk_extra["global_halt"] = bool(halted)
            if halted:
                halt_reason = str(why or "global_halt")
                risk_extra["halt_reason"] = halt_reason
            # Exposure vs caps — what Risk layout actually needs.
            rcfg = getattr(risk, "cfg", None) or cfg.risk
            exposure = float(risk._total_exposure())  # noqa: SLF001 — dashboard read
            cap_total = float(getattr(rcfg, "max_total_exposure_usdc", 0) or 0)
            risk_extra["exposure_usdc"] = round(exposure, 2)
            risk_extra["max_total_exposure_usdc"] = cap_total
            risk_extra["exposure_frac"] = round(exposure / cap_total, 3) if cap_total > 0 else None
            risk_extra["max_market_notional_usdc"] = float(
                getattr(rcfg, "max_market_notional_usdc", 0) or 0
            )
            risk_extra["order_error_rate"] = None
            attempts = int(getattr(risk, "_order_attempts", 0) or 0)
            errors = int(getattr(risk, "_order_errors", 0) or 0)
            if attempts > 0:
                risk_extra["order_error_rate"] = round(errors / attempts, 3)
                risk_extra["order_attempts"] = attempts

    # Live connectivity strip (Pulse).
    links: dict[str, Any] = {}
    md = getattr(engine, "md", None)
    if md is not None:
        links["market_ws"] = "up" if getattr(md, "connected", False) else "down"
    else:
        links["market_ws"] = "—"
    if paper:
        links["user_ws"] = "n/a"
        links["heartbeat"] = "n/a"
    else:
        user = getattr(engine, "user", None)
        if user is None:
            links["user_ws"] = "—"
        else:
            links["user_ws"] = "up" if getattr(user, "connected", False) else "down"
        gw = getattr(engine, "gateway", None)
        hb_fail = 0
        halt_after = int(getattr(cfg.risk, "heartbeat_halt_failures", 3) or 3)
        if gw is not None:
            with contextlib.suppress(Exception):
                hb_fail = int(getattr(gw, "heartbeat_failures", 0) or 0)
        links["heartbeat_failures"] = hb_fail
        if not getattr(cfg.engine, "heartbeat", True):
            links["heartbeat"] = "off"
        elif hb_fail >= halt_after:
            links["heartbeat"] = "down"
        elif hb_fail > 0:
            links["heartbeat"] = "degraded"
        else:
            links["heartbeat"] = "up"

    snap = build_snapshot_from_paths(
        db_path=cfg.paths.db,
        log_dir=cfg.paths.log_dir,
        metrics_log=metrics_log,
        paper=paper,
        capital_usdc=capital,
        kill_usdc=kill,
        live_markets=live_markets,
        risk_extra=risk_extra,
        metrics_bits=metrics_bits,
    )
    if snap.outage.get("outage_open"):
        links["outage"] = "open"
    else:
        links["outage"] = "clear"
    started = getattr(engine, "_started_at", None)
    if isinstance(started, (int, float)) and started > 0:
        links["uptime_s"] = int(max(0, time.time() - float(started)))
    snap.links = links

    dash = getattr(engine, "_live_dashboard", None)
    if dash is not None and getattr(dash, "url", ""):
        snap.url_hint = str(dash.url)

    # Escalate health when live links are blind (engine path only).
    if not halt_reason:
        if links.get("market_ws") == "down" or links.get("heartbeat") == "down":
            snap.health = "CRITICAL"
            parts = [k for k, v in links.items() if v == "down"]
            snap.health_detail = "Link down: " + ", ".join(parts)
        elif links.get("user_ws") == "down" or links.get("heartbeat") == "degraded":
            if snap.health in {"OK", "ACTIVE", "NO_DATA"}:
                snap.health = "WARN"
                snap.health_detail = "Link degraded — check user WS / heartbeat"

    if live_equity is not None:
        snap.equity = live_equity
    if live_pnl is not None:
        snap.daily_pnl = live_pnl
    if halt_reason:
        snap.health = "CRITICAL"
        snap.health_detail = f"Risk halt: {halt_reason}"
    snap.insights = build_insights(snap)
    return snap


def render_app_html(*, title: str = "Polymaker") -> str:
    """Single-page multi-layout dashboard (Pulse / Book / Risk / Tape)."""
    # Inline CSS+JS — no CDN. Cool slate + mint; not cream/purple.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
:root {{
  --bg: #0c1116;
  --panel: #141b22;
  --line: #243040;
  --ink: #e8eef4;
  --muted: #8b9aab;
  --mint: #3dffa8;
  --warn: #f0b429;
  --bad: #ff5c5c;
  --ok: #3dffa8;
  --live: #5b9dff;
  --paper: #c9a227;
  --font: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin:0; height:100%; background:var(--bg); color:var(--ink); font:15px/1.45 var(--font); }}
body {{
  background:
    radial-gradient(1200px 600px at 10% -10%, #1a2a38 0%, transparent 55%),
    radial-gradient(900px 500px at 100% 0%, #122018 0%, transparent 50%),
    var(--bg);
}}
.app {{ max-width: 1100px; margin: 0 auto; padding: 1.25rem 1.25rem 3rem; }}
.brand {{
  display:flex; align-items:baseline; justify-content:space-between; gap:1rem;
  margin-bottom: 1.25rem; border-bottom: 1px solid var(--line); padding-bottom: .85rem;
}}
@media (max-width: 560px) {{
  .brand {{ flex-direction: column; align-items:flex-start; gap:.35rem; }}
  .meta {{ text-align:left; }}
  .app {{ padding: 1rem 0.85rem 2.5rem; }}
}}
.brand h1 {{
  margin:0; font-size: clamp(1.6rem, 3vw, 2.1rem); letter-spacing: -0.03em;
  font-weight: 600;
}}
.brand h1 span {{ color: var(--mint); }}
.meta {{ color: var(--muted); font-size: .85rem; text-align:right; }}
.conn {{
  display:inline-block; width:.55rem; height:.55rem; border-radius:50%;
  margin-right:.35rem; vertical-align:middle; background: var(--warn);
}}
.conn-ok {{ background: var(--ok); box-shadow: 0 0 8px color-mix(in srgb, var(--ok) 55%, transparent); }}
.conn-bad {{ background: var(--bad); }}
.mode {{
  display:inline-block; padding:.15rem .55rem; border-radius:999px;
  font-size:.72rem; letter-spacing:.06em; font-weight:600; vertical-align:middle;
}}
.mode-LIVE {{ background: color-mix(in srgb, var(--live) 22%, transparent); color: var(--live); }}
.mode-PAPER {{ background: color-mix(in srgb, var(--paper) 22%, transparent); color: var(--paper); }}
.nav {{
  display:flex; gap:.4rem; flex-wrap:wrap; margin-bottom: 1.1rem;
}}
.nav button {{
  background: transparent; color: var(--muted); border: 1px solid var(--line);
  border-radius: 999px; padding: .4rem .9rem; cursor: pointer; font: inherit;
}}
.nav button.active {{
  color: var(--bg); background: var(--mint); border-color: var(--mint); font-weight: 600;
}}
.book-filters {{
  display:flex; gap:.4rem; flex-wrap:wrap; margin-bottom: .65rem;
}}
.book-filters button {{
  background: transparent; color: var(--muted); border: 1px solid var(--line);
  border-radius: 999px; padding: .35rem .8rem; cursor: pointer; font: inherit;
}}
.book-filters button.active {{
  color: var(--bg); background: var(--mint); border-color: var(--mint); font-weight: 600;
}}
.layout {{ display:none; animation: in .25s ease; }}
.layout.active {{ display:block; }}
@keyframes in {{ from {{ opacity:0; transform: translateY(4px); }} to {{ opacity:1; transform:none; }} }}
.hero {{
  display:grid; grid-template-columns: 1.2fr .8fr; gap: 1rem; margin-bottom: 1rem;
}}
@media (max-width: 720px) {{ .hero {{ grid-template-columns: 1fr; }} }}
.panel {{
  background: color-mix(in srgb, var(--panel) 92%, transparent);
  border: 1px solid var(--line); border-radius: 14px; padding: 1.1rem 1.2rem;
  backdrop-filter: blur(6px);
}}
.health-big {{
  font-size: clamp(2.4rem, 6vw, 3.6rem); font-weight: 650; letter-spacing: -0.04em;
  line-height: 1; margin: .35rem 0 .5rem;
}}
.health-OK, .health-ACTIVE {{ color: var(--ok); }}
.health-WARN {{ color: var(--warn); }}
.health-CRITICAL, .health-NO_DATA {{ color: var(--bad); }}
.health-CRITICAL {{
  animation: pulse-crit 1.6s ease-in-out infinite;
}}
@keyframes pulse-crit {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: .72; }}
}}
@media (prefers-reduced-motion: reduce) {{
  .health-CRITICAL {{ animation: none; }}
  .layout {{ animation: none; }}
}}
.detail {{ color: var(--muted); max-width: 36ch; }}
.statgrid {{
  display:grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: .65rem;
}}
.stat .k {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
.stat .v {{
  font-size: 1.35rem; font-weight: 600; font-variant-numeric: tabular-nums;
  font-family: var(--mono); margin-top: .15rem;
}}
.pos {{ color: var(--ok); }}
.neg {{ color: var(--bad); }}
.insights {{ margin-top: 1rem; }}
.insights li {{ margin: .35rem 0; color: var(--ink); }}
.insights li::marker {{ color: var(--mint); }}
table {{ width:100%; border-collapse: collapse; }}
th, td {{
  text-align:left; padding: .55rem .4rem; border-bottom: 1px solid var(--line);
  font-variant-numeric: tabular-nums;
}}
th {{
  font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
  font-weight: 500;
}}
code.copyable {{ cursor: pointer; border-bottom: 1px dotted color-mix(in srgb, var(--muted) 60%, transparent); }}
code.copyable:hover {{ color: var(--mint); }}
.toast {{
  position: fixed; bottom: 1.1rem; right: 1.1rem; padding: .45rem .75rem;
  background: #1c2733; border: 1px solid var(--line); border-radius: 8px;
  font-size: .78rem; color: var(--mint); opacity: 0; pointer-events: none;
  transition: opacity .2s ease;
}}
.toast.show {{ opacity: 1; }}
.regime {{
  font-family: var(--mono); font-size: .8rem; padding: .1rem .4rem; border-radius: 6px;
  background: #1c2733; color: var(--ink);
}}
.regime-QUIET {{ color: var(--ok); }}
.regime-TRENDING {{ color: var(--live); }}
.regime-EVENT {{ color: var(--warn); }}
.regime-HALTED, .regime-PAUSED {{ color: var(--bad); }}
.regime-REDUCE_ONLY {{ color: var(--warn); }}
.links {{
  display:flex; flex-wrap:wrap; gap:.5rem; margin: 0 0 1rem;
}}
.link-chip {{
  font-family: var(--mono); font-size: .72rem; letter-spacing: .04em;
  padding: .35rem .65rem; border-radius: 8px; border: 1px solid var(--line);
  background: color-mix(in srgb, var(--panel) 90%, transparent); color: var(--muted);
}}
.link-up {{ color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, var(--line)); }}
.link-down {{ color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }}
.link-degraded {{ color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--line)); }}
.link-open {{ color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }}
.link-clear, .link-na, .link-off, .link-dash {{ color: var(--muted); }}
.foot {{
  margin-top: 1.5rem; color: var(--muted); font-size: .78rem;
  display:flex; justify-content:space-between; gap:1rem; flex-wrap:wrap;
}}
.empty {{ color: var(--muted); padding: 1rem 0; }}
.bar {{
  height: .45rem; background: #1c2733; border-radius: 99px; overflow: hidden; margin-top: .45rem;
}}
.bar > i {{
  display:block; height:100%; width:0%; background: var(--mint); border-radius: 99px;
  transition: width .35s ease;
}}
.bar.warn > i {{ background: var(--warn); }}
.bar.bad > i {{ background: var(--bad); }}
.cap-row .k {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
.cap-row .v {{
  font-family: var(--mono); font-size: .95rem; font-variant-numeric: tabular-nums; margin-top: .1rem;
}}
</style>
</head>
<body>
<div class="app">
  <header class="brand">
    <h1>Poly<span>maker</span> <span id="mode" class="mode mode-PAPER">PAPER</span></h1>
    <div class="meta">
      <div><span id="conn" class="conn" title="snapshot link"></span><span id="clock">—</span></div>
      <div>auto-refresh 2s · keys 1–4 · localhost · <span id="age">—</span> · metrics <span id="metrics-age">—</span></div>
    </div>
  </header>

  <nav class="nav" id="nav">
    <button data-layout="pulse" class="active">Pulse</button>
    <button data-layout="book">Book</button>
    <button data-layout="risk">Risk</button>
    <button data-layout="tape">Tape</button>
  </nav>

  <section id="pulse" class="layout active">
    <div class="links" id="links"></div>
    <div class="links" id="regime-summary" style="margin-top:-0.35rem"></div>
    <div class="hero">
      <div class="panel">
        <div class="k" style="color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em">System health</div>
        <div id="health" class="health-big health-NO_DATA">…</div>
        <p id="health-detail" class="detail">Loading snapshot…</p>
      </div>
      <div class="panel">
        <div class="statgrid">
          <div class="stat"><div class="k">Daily PnL</div><div class="v" id="pnl">—</div></div>
          <div class="stat"><div class="k">Equity</div><div class="v" id="equity">—</div></div>
          <div class="stat"><div class="k">Capital</div><div class="v" id="capital">—</div></div>
          <div class="stat"><div class="k">Open orders</div><div class="v" id="orders">—</div></div>
          <div class="stat"><div class="k">Uptime</div><div class="v" id="uptime">—</div></div>
        </div>
      </div>
    </div>
    <div class="panel insights">
      <div class="k" style="color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:.4rem">What matters now</div>
      <ul id="insights"></ul>
    </div>
    <div class="panel" style="margin-top:1rem">
      <div class="cap-row">
        <div class="k">Daily PnL vs kill</div>
        <div class="v" id="pnl-kill-label">—</div>
        <div class="bar" id="pnl-kill-bar"><i id="pnl-kill-fill"></i></div>
      </div>
    </div>
  </section>

  <section id="book" class="layout">
    <div class="book-filters">
      <button type="button" id="book-filter-all" class="active">All</button>
      <button type="button" id="book-filter-inv">Inventory ≠ 0</button>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Market</th><th>Regime</th><th>Inv net</th><th>Reward acc.</th><th>Question</th></tr></thead>
        <tbody id="markets-body"><tr><td colspan="5" class="empty">No markets yet</td></tr></tbody>
      </table>
    </div>
  </section>

  <section id="risk" class="layout">
    <div class="statgrid" style="margin-bottom:1rem">
      <div class="panel stat"><div class="k">Inv peak |net|</div><div class="v" id="inv-peak">—</div></div>
      <div class="panel stat"><div class="k">Halted</div><div class="v" id="halted">—</div></div>
      <div class="panel stat"><div class="k">LLM paused</div><div class="v" id="paused">—</div></div>
      <div class="panel stat"><div class="k">Kill USDC</div><div class="v" id="kill">—</div></div>
    </div>
    <div class="panel" style="margin-bottom:1rem">
      <div class="cap-row">
        <div class="k">Total exposure / cap</div>
        <div class="v" id="exposure-label">—</div>
        <div class="bar" id="exposure-bar"><i id="exposure-fill"></i></div>
      </div>
      <div class="statgrid" style="margin-top:1rem">
        <div class="stat"><div class="k">Mkt notional cap</div><div class="v" id="mkt-cap">—</div></div>
        <div class="stat"><div class="k">Order err rate</div><div class="v" id="err-rate">—</div></div>
      </div>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Token</th><th>Size</th><th>Avg</th></tr></thead>
        <tbody id="pos-body"><tr><td colspan="3" class="empty">Flat</td></tr></tbody>
      </table>
    </div>
  </section>

  <section id="tape" class="layout">
    <div class="statgrid" style="margin-bottom:1rem">
      <div class="panel stat"><div class="k">Quotes</div><div class="v" id="n-quote">—</div></div>
      <div class="panel stat"><div class="k">Fills</div><div class="v" id="n-fill">—</div></div>
      <div class="panel stat"><div class="k">Cancels</div><div class="v" id="n-cancel">—</div></div>
      <div class="panel stat"><div class="k">Spread USDC</div><div class="v" id="spread">—</div></div>
    </div>
    <div class="statgrid" style="margin-bottom:1rem">
      <div class="panel stat"><div class="k">Quotes / fill</div><div class="v" id="qpf">—</div></div>
      <div class="panel stat"><div class="k">Cancel / quote</div><div class="v" id="cpq">—</div></div>
      <div class="panel stat"><div class="k">Fill rate</div><div class="v" id="fill-rate">—</div></div>
      <div class="panel stat"><div class="k">Markets</div><div class="v" id="n-markets">—</div></div>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Markout horizon</th><th>Mean</th></tr></thead>
        <tbody id="markout-body"><tr><td colspan="2" class="empty">—</td></tr></tbody>
      </table>
    </div>
  </section>

  <footer class="foot">
    <span>Layouts: Pulse (1) · Book (2) · Risk (3) · Tape (4) · click market id to copy</span>
    <span><span id="ver"></span> <span id="url-hint"></span> <span id="gen">—</span></span>
  </footer>
</div>
<div class="toast" id="toast">Copied</div>
<script>
(function() {{
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d=2) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : Number(n).toFixed(d);
  const esc = (v) => String(v ?? "").replace(/[&<>'"]/g, (c) => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }}[c]));
  const classToken = (v) => String(v ?? "—").replace(/[^a-z0-9_-]/gi, "-");
  const money = (n) => {{
    if (n === null || n === undefined) return "—";
    const v = Number(n);
    const s = (v >= 0 ? "+" : "") + v.toFixed(2);
    return s;
  }};
  function toast(msg) {{
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => t.classList.remove("show"), 1200);
  }}
  async function copyText(text) {{
    try {{
      await navigator.clipboard.writeText(text);
      toast("Copied " + text);
    }} catch (e) {{
      // Clipboard is unavailable in some hardened / older localhost contexts.
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      toast(copied ? ("Copied " + text) : "Copy failed");
    }}
  }}

  function showLayout(name) {{
    const b = document.querySelector(`#nav button[data-layout="${{name}}"]`);
    if (!b) return;
    document.querySelectorAll("#nav button").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".layout").forEach(l => l.classList.remove("active"));
    b.classList.add("active");
    $(name).classList.add("active");
    try {{ localStorage.setItem("pm_layout", name); }} catch (e) {{}}
    if (location.hash.replace(/^#/, "") !== name) {{
      history.replaceState(null, "", "#" + name);
    }}
  }}

  let bookFilter = "all";
  try {{ bookFilter = localStorage.getItem("pm_book_filter") || "all"; }} catch (e) {{}}
  function setBookFilter(mode) {{
    bookFilter = mode;
    try {{ localStorage.setItem("pm_book_filter", mode); }} catch (e) {{}}
    $("book-filter-all").classList.toggle("active", mode === "all");
    $("book-filter-inv").classList.toggle("active", mode === "inv");
    if (window.__lastMarkets) renderMarkets(window.__lastMarkets);
  }}
  function renderMarkets(markets) {{
    const mb = $("markets-body");
    let rows = markets || [];
    if (bookFilter === "inv") {{
      rows = rows.filter(m => Math.abs(Number(m.inventory_net) || 0) > 1e-9);
    }}
    if (!rows.length) {{
      mb.innerHTML = '<tr><td colspan="5" class="empty">' +
        (bookFilter === "inv" ? "No inventory drift" : "No markets yet") + "</td></tr>";
      return;
    }}
    mb.innerHTML = rows.map(m => {{
      const inv = Number(m.inventory_net) || 0;
      const invCls = inv > 0 ? "pos" : (inv < 0 ? "neg" : "");
      const marketId = String(m.id || "—");
      const copyId = String(m.condition_id || marketId);
      const regime = String(m.regime || "—");
      const cooloff = Number(m.cooloff_s) || 0;
      return `<tr>
      <td><code class="copyable" data-copy="${{esc(copyId)}}" title="click to copy full condition id">${{esc(marketId)}}</code></td>
      <td><span class="regime regime-${{classToken(regime)}}">${{esc(regime)}}${{cooloff ? " ·" + esc(cooloff) + "s" : ""}}</span></td>
      <td class="${{invCls}}">${{fmt(m.inventory_net, 2)}}</td>
      <td>${{fmt(m.reward_accrual, 3)}}</td>
      <td style="color:var(--muted);max-width:28ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{esc(m.question)}}</td>
    </tr>`;
    }}).join("");
    mb.querySelectorAll("code.copyable").forEach(el => {{
      el.addEventListener("click", () => copyText(el.dataset.copy || el.textContent));
    }});
  }}
  $("book-filter-all").addEventListener("click", () => setBookFilter("all"));
  $("book-filter-inv").addEventListener("click", () => setBookFilter("inv"));
  $("book-filter-all").classList.toggle("active", bookFilter === "all");
  $("book-filter-inv").classList.toggle("active", bookFilter === "inv");

  document.querySelectorAll("#nav button").forEach(btn => {{
    btn.addEventListener("click", () => showLayout(btn.dataset.layout));
  }});
  document.addEventListener("keydown", (e) => {{
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
    const map = {{ "1": "pulse", "2": "book", "3": "risk", "4": "tape" }};
    if (map[e.key]) showLayout(map[e.key]);
    if (e.key === "Escape") showLayout("pulse");
    if (e.key === "?" || (e.shiftKey && e.key === "/")) {{
      toast("1–4 layouts · Esc pulse · i inventory · u copy URL · ? help");
    }}
    if (e.key === "i" || e.key === "I") {{
      if (bookFilter === "inv") setBookFilter("all");
      else setBookFilter("inv");
      showLayout("book");
    }}
    if (e.key === "u" || e.key === "U") {{
      const hint = ($("url-hint").textContent || "").replace(/\\s*·\\s*$/, "").trim();
      copyText(hint || location.href.split("#")[0]);
    }}
  }});
  window.addEventListener("hashchange", () => {{
    const h = location.hash.replace(/^#/, "");
    if (h) showLayout(h);
  }});
  (function initLayout() {{
    const fromHash = location.hash.replace(/^#/, "");
    if (fromHash && document.querySelector(`#nav button[data-layout="${{fromHash}}"]`)) {{
      showLayout(fromHash);
      return;
    }}
    try {{
      const saved = localStorage.getItem("pm_layout");
      if (saved) showLayout(saved);
    }} catch (e) {{}}
  }})();

  let lastSnap = null;
  function paintAge() {{
    if (!lastSnap) return;
    const ageEl = $("age");
    if (typeof lastSnap.ts === "number") {{
      const secs = Math.max(0, Math.round(Date.now() / 1000 - lastSnap.ts));
      ageEl.textContent = secs <= 2 ? "fresh" : secs + "s ago";
      ageEl.style.color = secs > 8 ? "var(--bad)" : (secs > 4 ? "var(--warn)" : "var(--muted)");
    }} else {{
      ageEl.textContent = "—";
    }}
    const ma = $("metrics-age");
    if (typeof lastSnap.metrics_age_s === "number" && typeof lastSnap.ts === "number") {{
      const a = lastSnap.metrics_age_s + Math.max(0, Date.now() / 1000 - lastSnap.ts);
      ma.textContent = a < 5 ? "live" : Math.round(a) + "s ago";
      ma.style.color = a > 120 ? "var(--bad)" : (a > 30 ? "var(--warn)" : "var(--muted)");
    }} else {{
      ma.textContent = "—";
      ma.style.color = "var(--muted)";
    }}
  }}
  function paint(s) {{
    lastSnap = s;
    document.title = "Polymaker · " + (s.health || "…") + " · " + (s.mode || "");
    const mode = $("mode");
    mode.textContent = s.mode;
    mode.className = "mode mode-" + s.mode;
    const h = $("health");
    h.textContent = s.health;
    h.className = "health-big health-" + s.health;
    $("health-detail").textContent = s.health_detail || "";
    const pnl = $("pnl");
    pnl.textContent = money(s.daily_pnl);
    pnl.className = "v " + ((s.daily_pnl || 0) >= 0 ? "pos" : "neg");
    $("equity").textContent = fmt(s.equity, 2);
    $("capital").textContent = fmt(s.capital_usdc, 0);
    $("orders").textContent = String(s.open_orders);
    const up = (s.links && s.links.uptime_s);
    if (typeof up === "number") {{
      const h = Math.floor(up / 3600);
      const m = Math.floor((up % 3600) / 60);
      const sec = up % 60;
      $("uptime").textContent = h > 0
        ? (h + "h " + m + "m")
        : (m > 0 ? (m + "m " + sec + "s") : (sec + "s"));
    }} else {{
      $("uptime").textContent = "—";
    }}
    $("clock").textContent = s.generated_at;
    $("gen").textContent = "snapshot " + s.generated_at;
    const ver = $("ver");
    ver.textContent = s.version ? ("v" + s.version + " ·") : "";
    const uh = $("url-hint");
    if (s.url_hint) {{
      uh.textContent = s.url_hint + " ·";
    }} else {{
      uh.textContent = "";
    }}
    paintAge();

    const linkBox = $("links");
    const L = s.links || {{}};
    const order = ["market_ws", "user_ws", "heartbeat", "outage"];
    const labels = {{ market_ws: "market WS", user_ws: "user WS", heartbeat: "heartbeat", outage: "outage" }};
    linkBox.innerHTML = order.filter(k => k in L).map(k => {{
      const st = String(L[k]);
      const stCls = st === "n/a" ? "na" : (st === "—" ? "dash" : classToken(st));
      const cls = "link-chip link-" + stCls;
      const extra = (k === "heartbeat" && L.heartbeat_failures) ? " ·" + L.heartbeat_failures : "";
      return `<span class="${{cls}}">${{esc(labels[k] || k)}} · ${{esc(st)}}${{esc(extra)}}</span>`;
    }}).join("");

    const rs = $("regime-summary");
    const counts = {{}};
    (s.markets || []).forEach(m => {{
      const r = m.regime || "—";
      counts[r] = (counts[r] || 0) + 1;
    }});
    const rOrder = ["HALTED", "PAUSED", "EVENT", "REDUCE_ONLY", "TRENDING", "QUIET", "—"];
    const keys = rOrder.filter(k => counts[k]).concat(Object.keys(counts).filter(k => !rOrder.includes(k)));
    rs.innerHTML = keys.length
      ? keys.map(k => `<span class="link-chip regime regime-${{classToken(k)}}">${{esc(k)}} · ${{esc(counts[k])}}</span>`).join("")
      : "";

    const ul = $("insights");
    ul.innerHTML = "";
    (s.insights || []).forEach(t => {{
      const li = document.createElement("li");
      li.textContent = t;
      ul.appendChild(li);
    }});

    const kill = (s.risk && s.risk.daily_loss_kill_usdc);
    const pnlV = s.daily_pnl;
    if (typeof kill === "number" && kill > 0 && typeof pnlV === "number") {{
      // 0% = at -kill, 50% = flat, 100% = +kill (symmetric view of headroom).
      const frac = Math.max(0, Math.min(1, (pnlV + kill) / (2 * kill)));
      $("pnl-kill-label").textContent = money(pnlV) + " / kill -" + fmt(kill, 0);
      $("pnl-kill-fill").style.width = Math.round(frac * 100) + "%";
      const bar = $("pnl-kill-bar");
      bar.className = "bar" + (pnlV <= -0.7 * kill ? " bad" : (pnlV < 0 ? " warn" : ""));
    }} else {{
      $("pnl-kill-label").textContent = "—";
      $("pnl-kill-fill").style.width = "0%";
      $("pnl-kill-bar").className = "bar";
    }}

    window.__lastMarkets = s.markets || [];
    renderMarkets(window.__lastMarkets);

    $("inv-peak").textContent = fmt(s.inventory_peak, 1);
    $("halted").textContent = String((s.risk && s.risk.halted_markets) || 0);
    $("paused").textContent = String((s.risk && s.risk.llm_paused) || 0);
    $("kill").textContent = fmt(s.risk && s.risk.daily_loss_kill_usdc, 0);

    const R = s.risk || {{}};
    const exp = R.exposure_usdc;
    const cap = R.max_total_exposure_usdc;
    const frac = (typeof R.exposure_frac === "number") ? R.exposure_frac
      : (cap > 0 && typeof exp === "number") ? (exp / cap) : null;
    if (typeof exp === "number" && cap > 0) {{
      $("exposure-label").textContent = fmt(exp, 1) + " / " + fmt(cap, 0) +
        (frac !== null ? "  (" + Math.round(frac * 100) + "%)" : "");
      const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
      $("exposure-fill").style.width = pct + "%";
      const bar = $("exposure-bar");
      bar.className = "bar" + (frac >= 0.9 ? " bad" : (frac >= 0.7 ? " warn" : ""));
    }} else {{
      $("exposure-label").textContent = "—";
      $("exposure-fill").style.width = "0%";
      $("exposure-bar").className = "bar";
    }}
    $("mkt-cap").textContent = fmt(R.max_market_notional_usdc, 0);
    $("err-rate").textContent = (typeof R.order_error_rate === "number")
      ? (Math.round(R.order_error_rate * 1000) / 10) + "%"
      : "—";

    const pb = $("pos-body");
    if (!s.positions || !s.positions.length) {{
      pb.innerHTML = '<tr><td colspan="3" class="empty">Flat</td></tr>';
    }} else {{
      const rows = s.positions.slice().sort((a, b) => Math.abs(b.size||0) - Math.abs(a.size||0));
      pb.innerHTML = rows.map(p => {{
        const sz = Number(p.size) || 0;
        const cls = sz > 0 ? "pos" : (sz < 0 ? "neg" : "");
        return `<tr>
        <td><code>${{esc(p.token)}}</code></td><td class="${{cls}}">${{fmt(p.size, 2)}}</td><td>${{fmt(p.avg_price, 3)}}</td>
      </tr>`;
      }}).join("");
    }}

    $("n-quote").textContent = String(s.n_quote);
    $("n-fill").textContent = String(s.n_fill);
    $("n-cancel").textContent = String(s.n_cancel);
    $("spread").textContent = fmt(s.realized_spread_usdc, 4);
    const nq = Number(s.n_quote) || 0;
    const nf = Number(s.n_fill) || 0;
    const nc = Number(s.n_cancel) || 0;
    $("qpf").textContent = nf > 0 ? fmt(nq / nf, 1) : (nq > 0 ? "∞" : "—");
    $("cpq").textContent = nq > 0 ? fmt(nc / nq, 2) : "—";
    $("fill-rate").textContent = nq > 0 ? (Math.round((nf / nq) * 1000) / 10) + "%" : "—";
    $("n-markets").textContent = String(s.n_markets || 0);
    const mk = s.markout || {{}};
    const markoutKeys = ["30s", "120s", "300s"].filter(k => k in mk);
    const mbody = $("markout-body");
    if (!markoutKeys.length) {{
      mbody.innerHTML = '<tr><td colspan="2" class="empty">No markouts yet</td></tr>';
    }} else {{
      mbody.innerHTML = markoutKeys.map(k => {{
        const v = mk[k];
        const cls = (typeof v === "number" && v < 0) ? "neg" : ((typeof v === "number" && v > 0) ? "pos" : "");
        return `<tr><td>${{k}}</td><td class="${{cls}}">${{fmt(v, 5)}}</td></tr>`;
      }}).join("");
    }}
  }}

  let tickInFlight = false;
  async function tick() {{
    if (document.visibilityState === "hidden" || tickInFlight) return;
    tickInFlight = true;
    const dot = $("conn");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {{
      const r = await fetch("/api/snapshot", {{ cache: "no-store", signal: controller.signal }});
      if (!r.ok) throw new Error("HTTP " + r.status);
      paint(await r.json());
      dot.className = "conn conn-ok";
      dot.title = "snapshot OK";
    }} catch (e) {{
      dot.className = "conn conn-bad";
      dot.title = "snapshot failed";
      const health = $("health");
      health.textContent = "OFFLINE";
      health.className = "health-big health-CRITICAL";
      $("health-detail").textContent = "Waiting for bot snapshot… (" + e.message + ")";
    }} finally {{
      clearTimeout(timeout);
      tickInFlight = false;
    }}
  }}
  document.addEventListener("visibilitychange", () => {{
    if (document.visibilityState === "visible") tick();
  }});
  tick();
  setInterval(tick, 2000);
  setInterval(paintAge, 1000);
}})();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        if args and "api/snapshot" in str(args[0]):
            return
        log.debug("dashboard_http %s", fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self) -> dict[str, Any]:
        fn = getattr(self.server, "snapshot_fn", None)
        if not callable(fn):
            raise RuntimeError("no snapshot function configured")
        payload = fn()
        if not isinstance(payload, dict):
            raise TypeError("snapshot function returned a non-object payload")
        if "error" in payload:
            raise RuntimeError("snapshot function returned an error payload")
        self.server.last_snapshot = payload  # type: ignore[attr-defined]
        self.server.last_snapshot_at = time.time()  # type: ignore[attr-defined]
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send(200, render_app_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/snapshot":
            try:
                payload = self._snapshot()
                body = json.dumps(payload, default=str).encode("utf-8")
                self._send(200, body, "application/json")
            except Exception:  # noqa: BLE001 - handler must keep serving after a bad snapshot
                log.exception("dashboard_snapshot_failed")
                self._send(
                    503,
                    b'{"error":"snapshot unavailable"}',
                    "application/json",
                )
            return
        if path == "/healthz":
            # A health probe needs to exercise the same snapshot path as the UI.
            # Otherwise an old successful browser refresh could make healthz look
            # healthy forever after the engine/dashboard data path has failed.
            try:
                self._snapshot()
            except Exception:  # noqa: BLE001 - return a reliable probe response
                log.exception("dashboard_health_snapshot_failed")
                self._send(
                    503,
                    b'{"ok":false,"error":"snapshot unavailable"}',
                    "application/json",
                )
                return

            last = getattr(self.server, "last_snapshot", None)
            last_at = getattr(self.server, "last_snapshot_at", None)
            info: dict[str, Any] = {"ok": True}
            if isinstance(last, dict):
                info["health"] = last.get("health")
                info["mode"] = last.get("mode")
                info["ts"] = last.get("ts")
                if last.get("version"):
                    info["version"] = last.get("version")
            if isinstance(last_at, (int, float)):
                info["snapshot_age_s"] = round(time.time() - float(last_at), 2)
            if info.get("health") == "CRITICAL":
                info["ok"] = False
            ts = info.get("ts")
            if isinstance(ts, (int, float)):
                source_age = time.time() - float(ts)
                info["source_age_s"] = round(max(0.0, source_age), 2)
                if source_age > _SNAPSHOT_STALE_S:
                    info["ok"] = False
                    info["error"] = "snapshot data is stale"
            elif last is None:
                info["ok"] = False
                info["error"] = "snapshot unavailable"
            body = json.dumps(info).encode("utf-8")
            self._send(200 if info["ok"] else 503, body, "application/json")
            return
        self.send_error(404)


def _pick_port(host: str, preferred: int, span: int = 15) -> int:
    if not 0 <= preferred <= 65535:
        raise ValueError("dashboard port must be in the range 0..65535")
    # Port 0 asks the OS for an ephemeral port. Its concrete value is only
    # known after ThreadingHTTPServer binds, so return it unchanged here.
    if preferred == 0:
        return 0
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(preferred, preferred + span):
        if port > 65535:
            break
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError(f"no free dashboard port near {preferred}")


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer variant for the supported ``::1`` bind target."""

    address_family = socket.AF_INET6


class LiveDashboard:
    """Background ThreadingHTTPServer for the operator UI."""

    def __init__(
        self,
        snapshot_fn: Callable[[], dict[str, Any]],
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        open_browser: bool = True,
    ) -> None:
        self.snapshot_fn = snapshot_fn
        self.host = _require_loopback_host(host)
        self.port = port
        self.open_browser = open_browser
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        port = _pick_port(self.host, self.port)
        server_cls = _IPv6ThreadingHTTPServer if ":" in self.host else ThreadingHTTPServer
        httpd = server_cls((self.host, port), _Handler)
        httpd.snapshot_fn = self.snapshot_fn  # type: ignore[attr-defined]
        httpd.last_snapshot = None  # type: ignore[attr-defined]
        httpd.last_snapshot_at = None  # type: ignore[attr-defined]
        self._httpd = httpd
        # An ephemeral port (0) is resolved only after server construction.
        self.port = int(httpd.server_address[1])
        url_host = f"[{self.host}]" if ":" in self.host else self.host
        self.url = f"http://{url_host}:{self.port}/"
        t = threading.Thread(target=httpd.serve_forever, name="polymaker-dashboard", daemon=True)
        t.start()
        self._thread = t
        log.info("dashboard_listening", url=self.url)
        if self.open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(self.url)
        return self.url

    def stop(self) -> None:
        if self._httpd is None:
            return
        with contextlib.suppress(Exception):
            self._httpd.shutdown()
        with contextlib.suppress(Exception):
            self._httpd.server_close()
        self._httpd = None
        self._thread = None
        log.info("dashboard_stopped")


def start_for_engine(
    engine: Any,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> LiveDashboard:
    """Attach a live dashboard to a running Engine and open the browser."""

    def _snap() -> dict[str, Any]:
        return build_snapshot_from_engine(engine).as_dict()

    dash = LiveDashboard(_snap, host=host, port=port, open_browser=open_browser)
    dash.start()
    engine._live_dashboard = dash  # type: ignore[attr-defined]
    return dash
