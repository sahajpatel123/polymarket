"""Live localhost operator dashboard — opens when the bot starts.

Serves a single-page multi-layout UI + JSON snapshot API on 127.0.0.1.
No heavy framework: stdlib HTTP server in a daemon thread, auto-refresh
in the browser. Designed for glanceable ops (Pulse / Book / Risk / Tape).
"""

from __future__ import annotations

import contextlib
import json
import logging
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


def _read_pnl(db_path: Path) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "equity": None,
        "daily_pnl": None,
        "net_cash": None,
        "inventory_value": None,
    }
    if not db_path.exists():
        return out
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
            out["daily_pnl"] = (
                float(row["daily_pnl"]) if row["daily_pnl"] is not None else None
            )
            out["net_cash"] = (
                float(row["net_cash"]) if row["net_cash"] is not None else None
            )
            out["inventory_value"] = (
                float(row["inventory_value"])
                if row["inventory_value"] is not None
                else None
            )
    except Exception:
        pass
    return out


def _read_state_store(db_path: Path) -> tuple[int, list[dict[str, Any]]]:
    if not db_path.exists():
        return 0, []
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
        return open_orders, positions
    except Exception:
        return 0, []


def _metrics_bits(metrics_log: Path) -> dict[str, Any]:
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
        from polymaker.metrics.analyze import analyze

        rep = analyze(metrics_log)
        return {
            "n_quote": rep.n_quote,
            "n_fill": rep.n_fill,
            "n_cancel": rep.n_cancel,
            "n_bad": rep.n_bad,
            "realized_spread_usdc": round(rep.realized_spread_usdc, 4),
            "inventory_peak": round(rep.inventory_drift_abs_peak, 2),
            "markout": {k: round(v, 6) for k, v in rep.markout.items()},
            "markets": sorted(rep.markets),
            "reward_accrual": {
                k: round(v, 4) for k, v in rep.reward_accrual_usdc.items()
            },
            "inventory_net": {
                k: round(v, 4) for k, v in rep.inventory_net_end.items()
            },
        }
    except Exception:
        return empty


def build_insights(snap: DashboardSnapshot) -> list[str]:
    """Short, operator-facing sentences — the 'smart' layer."""
    tips: list[str] = []
    if snap.risk.get("global_halt"):
        tips.append(
            f"Global risk halt — {snap.risk.get('halt_reason') or 'check kill / error rate'}."
        )
    if snap.mode == "PAPER" and snap.n_fill == 0 and snap.n_quote > 50:
        tips.append("Paper is quoting but has no fills — reward farming posture, not PnL proof.")
    if snap.health == "CRITICAL" and not snap.risk.get("global_halt"):
        tips.append("Fix outage before trusting quotes: check connectivity / collector.")
    if snap.daily_pnl is not None and snap.daily_pnl < 0 and snap.n_fill > 0:
        tips.append("Negative day with fills — check markouts on Risk/Tape; size may be too large.")
    if snap.inventory_peak > 100:
        tips.append("Inventory peak is elevated — prefer exits / REDUCE_ONLY over adding.")
    if snap.open_orders == 0 and snap.n_markets > 0 and snap.health in {"OK", "ACTIVE"}:
        tips.append("No open orders despite markets — regime may be HALTED/EVENT or WS blind.")
    if not tips:
        tips.append("Steady state. Watch Pulse for health color; switch to Book for per-market drift.")
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
) -> DashboardSnapshot:
    db = Path(db_path)
    logs = Path(log_dir)
    mlog = Path(metrics_log)
    pnl = _read_pnl(db)
    open_orders, positions = _read_state_store(db)
    bits = _metrics_bits(mlog)
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
    for cid, meta in getattr(engine, "metas", {}).items():
        regime = "QUIET"
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

    snap = build_snapshot_from_paths(
        db_path=cfg.paths.db,
        log_dir=cfg.paths.log_dir,
        metrics_log=metrics_log,
        paper=paper,
        capital_usdc=capital,
        kill_usdc=kill,
        live_markets=live_markets,
        risk_extra=risk_extra,
    )
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
.regime {{
  font-family: var(--mono); font-size: .8rem; padding: .1rem .4rem; border-radius: 6px;
  background: #1c2733; color: var(--ink);
}}
.foot {{
  margin-top: 1.5rem; color: var(--muted); font-size: .78rem;
  display:flex; justify-content:space-between; gap:1rem; flex-wrap:wrap;
}}
.empty {{ color: var(--muted); padding: 1rem 0; }}
</style>
</head>
<body>
<div class="app">
  <header class="brand">
    <h1>Poly<span>maker</span> <span id="mode" class="mode mode-PAPER">PAPER</span></h1>
    <div class="meta">
      <div><span id="conn" class="conn" title="snapshot link"></span><span id="clock">—</span></div>
      <div>auto-refresh 2s · keys 1–4 · localhost</div>
    </div>
  </header>

  <nav class="nav" id="nav">
    <button data-layout="pulse" class="active">Pulse</button>
    <button data-layout="book">Book</button>
    <button data-layout="risk">Risk</button>
    <button data-layout="tape">Tape</button>
  </nav>

  <section id="pulse" class="layout active">
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
        </div>
      </div>
    </div>
    <div class="panel insights">
      <div class="k" style="color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:.4rem">What matters now</div>
      <ul id="insights"></ul>
    </div>
  </section>

  <section id="book" class="layout">
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
    <div class="panel">
      <table>
        <thead><tr><th>Markout horizon</th><th>Mean</th></tr></thead>
        <tbody id="markout-body"><tr><td colspan="2" class="empty">—</td></tr></tbody>
      </table>
    </div>
  </section>

  <footer class="foot">
    <span>Layouts: Pulse (now) · Book (markets) · Risk (inventory) · Tape (flow)</span>
    <span id="gen">—</span>
  </footer>
</div>
<script>
(function() {{
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d=2) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : Number(n).toFixed(d);
  const money = (n) => {{
    if (n === null || n === undefined) return "—";
    const v = Number(n);
    const s = (v >= 0 ? "+" : "") + v.toFixed(2);
    return s;
  }};

  document.querySelectorAll(".nav button").forEach(btn => {{
    btn.addEventListener("click", () => {{
      document.querySelectorAll(".nav button").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".layout").forEach(l => l.classList.remove("active"));
      btn.classList.add("active");
      $(btn.dataset.layout).classList.add("active");
      try {{ localStorage.setItem("pm_layout", btn.dataset.layout); }} catch (e) {{}}
    }});
  }});
  document.addEventListener("keydown", (e) => {{
    const map = {{ "1": "pulse", "2": "book", "3": "risk", "4": "tape" }};
    if (map[e.key]) {{
      const b = document.querySelector(`.nav button[data-layout="${{map[e.key]}}"]`);
      if (b) b.click();
    }}
  }});
  try {{
    const saved = localStorage.getItem("pm_layout");
    if (saved) {{
      const b = document.querySelector(`.nav button[data-layout="${{saved}}"]`);
      if (b) b.click();
    }}
  }} catch (e) {{}}

  function paint(s) {{
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
    $("clock").textContent = s.generated_at;
    $("gen").textContent = "snapshot " + s.generated_at;

    const ul = $("insights");
    ul.innerHTML = "";
    (s.insights || []).forEach(t => {{
      const li = document.createElement("li");
      li.textContent = t;
      ul.appendChild(li);
    }});

    const mb = $("markets-body");
    if (!s.markets || !s.markets.length) {{
      mb.innerHTML = '<tr><td colspan="5" class="empty">No markets yet</td></tr>';
    }} else {{
      mb.innerHTML = s.markets.map(m => `<tr>
        <td><code>${{m.id}}</code></td>
        <td><span class="regime">${{m.regime || "—"}}${{m.cooloff_s ? " ·" + m.cooloff_s + "s" : ""}}</span></td>
        <td>${{fmt(m.inventory_net, 2)}}</td>
        <td>${{fmt(m.reward_accrual, 3)}}</td>
        <td style="color:var(--muted);max-width:28ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{m.question || ""}}</td>
      </tr>`).join("");
    }}

    $("inv-peak").textContent = fmt(s.inventory_peak, 1);
    $("halted").textContent = String((s.risk && s.risk.halted_markets) || 0);
    $("paused").textContent = String((s.risk && s.risk.llm_paused) || 0);
    $("kill").textContent = fmt(s.risk && s.risk.daily_loss_kill_usdc, 0);

    const pb = $("pos-body");
    if (!s.positions || !s.positions.length) {{
      pb.innerHTML = '<tr><td colspan="3" class="empty">Flat</td></tr>';
    }} else {{
      pb.innerHTML = s.positions.map(p => `<tr>
        <td><code>${{p.token}}</code></td><td>${{fmt(p.size, 2)}}</td><td>${{fmt(p.avg_price, 3)}}</td>
      </tr>`).join("");
    }}

    $("n-quote").textContent = String(s.n_quote);
    $("n-fill").textContent = String(s.n_fill);
    $("n-cancel").textContent = String(s.n_cancel);
    $("spread").textContent = fmt(s.realized_spread_usdc, 4);
    const mk = s.markout || {{}};
    const keys = ["30s", "120s", "300s"].filter(k => k in mk);
    const mbody = $("markout-body");
    if (!keys.length) {{
      mbody.innerHTML = '<tr><td colspan="2" class="empty">No markouts yet</td></tr>';
    }} else {{
      mbody.innerHTML = keys.map(k => `<tr><td>${{k}}</td><td>${{fmt(mk[k], 5)}}</td></tr>`).join("");
    }}
  }}

  async function tick() {{
    const dot = $("conn");
    try {{
      const r = await fetch("/api/snapshot", {{ cache: "no-store" }});
      if (!r.ok) throw new Error("HTTP " + r.status);
      paint(await r.json());
      dot.className = "conn conn-ok";
      dot.title = "snapshot OK";
    }} catch (e) {{
      dot.className = "conn conn-bad";
      dot.title = "snapshot failed";
      $("health-detail").textContent = "Waiting for bot snapshot… (" + e.message + ")";
    }}
  }}
  tick();
  setInterval(tick, 2000);
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

    def _snapshot(self) -> dict[str, Any]:
        fn = getattr(self.server, "snapshot_fn", None)
        if not callable(fn):
            return {"error": "no snapshot_fn"}
        return fn()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            body = render_app_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            try:
                payload = self._snapshot()
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                err = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return
        if path == "/healthz":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def _pick_port(host: str, preferred: int, span: int = 15) -> int:
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError(f"no free dashboard port near {preferred}")


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
        self.host = host
        self.port = port
        self.open_browser = open_browser
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        port = _pick_port(self.host, self.port)
        httpd = ThreadingHTTPServer((self.host, port), _Handler)
        httpd.snapshot_fn = self.snapshot_fn  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = port
        self.url = f"http://{self.host}:{port}/"
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
