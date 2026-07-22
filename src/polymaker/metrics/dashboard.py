"""Render a simple health dashboard HTML from T1-01 metrics (T1-08)."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymaker.metrics.analyze import MetricsReport, analyze


def _esc(v: Any) -> str:
    return html.escape(str(v))


def render_dashboard(report: MetricsReport, *, title: str = "polymaker metrics") -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    markouts = report.markout
    markout_n = report.markout_n
    markets = sorted(report.markets) or ["—"]
    health = "OK"
    if report.n_bad > 0:
        health = "LOG_ERRORS"
    elif report.n_quote == 0 and report.n_fill == 0:
        health = "NO_DATA"
    elif report.inventory_drift_abs_peak > 0 and report.n_fill > 0:
        # informational — still OK for glance
        health = "ACTIVE"

    rows_markout = "".join(
        f"<tr><td>{_esc(h)}</td><td>{_esc(markouts.get(h, 0.0))}</td>"
        f"<td>{_esc(markout_n.get(h, 0))}</td></tr>"
        for h in ("30s", "120s", "300s")
    )
    inv_rows = "".join(
        f"<tr><td><code>{_esc(m[:18])}…</code></td><td>{_esc(v)}</td></tr>"
        if len(m) > 18
        else f"<tr><td><code>{_esc(m)}</code></td><td>{_esc(v)}</td></tr>"
        for m, v in sorted(report.inventory_net_end.items())
    ) or "<tr><td colspan='2'>—</td></tr>"
    reward_rows = "".join(
        f"<tr><td><code>{_esc(m[:18])}…</code></td><td>{_esc(round(v, 4))}</td></tr>"
        if len(m) > 18
        else f"<tr><td><code>{_esc(m)}</code></td><td>{_esc(round(v, 4))}</td></tr>"
        for m, v in sorted(report.reward_accrual_usdc.items())
    ) or "<tr><td colspan='2'>—</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(title)}</title>
<style>
  :root {{ color-scheme: light; --bg:#f4f1ea; --ink:#1a1a1a; --muted:#5c5c5c; --ok:#1b7f4e; --warn:#9a5b00; --bad:#9b1c1c; --card:#fff; }}
  body {{ margin:0; font:15px/1.45 "IBM Plex Sans", "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }}
  main {{ max-width:720px; margin:2rem auto; padding:0 1rem 3rem; }}
  h1 {{ font-size:1.4rem; margin:0 0 .25rem; letter-spacing:-0.02em; }}
  .sub {{ color:var(--muted); margin-bottom:1.5rem; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.75rem; margin-bottom:1.25rem; }}
  .card {{ background:var(--card); border:1px solid #ddd6c8; border-radius:8px; padding:.85rem 1rem; }}
  .card .k {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .card .v {{ font-size:1.35rem; font-weight:600; margin-top:.15rem; font-variant-numeric:tabular-nums; }}
  .health-OK,.health-ACTIVE {{ color:var(--ok); }}
  .health-NO_DATA {{ color:var(--warn); }}
  .health-LOG_ERRORS {{ color:var(--bad); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid #ddd6c8; border-radius:8px; overflow:hidden; margin-bottom:1.25rem; }}
  th,td {{ text-align:left; padding:.55rem .75rem; border-bottom:1px solid #eee8dc; font-variant-numeric:tabular-nums; }}
  th {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); background:#faf8f3; }}
  h2 {{ font-size:.95rem; margin:1.4rem 0 .5rem; }}
  code {{ font-size:.85em; }}
</style>
</head>
<body>
<main>
  <h1>{_esc(title)}</h1>
  <p class="sub">Generated {_esc(now)} · source <code>{_esc(report.path)}</code></p>

  <div class="grid">
    <div class="card"><div class="k">Health</div><div class="v health-{_esc(health)}">{_esc(health)}</div></div>
    <div class="card"><div class="k">Quotes</div><div class="v">{_esc(report.n_quote)}</div></div>
    <div class="card"><div class="k">Fills</div><div class="v">{_esc(report.n_fill)}</div></div>
    <div class="card"><div class="k">Cancels</div><div class="v">{_esc(report.n_cancel)}</div></div>
    <div class="card"><div class="k">Realized spread</div><div class="v">{_esc(round(report.realized_spread_usdc, 4))}</div></div>
    <div class="card"><div class="k">Inv. peak |net|</div><div class="v">{_esc(round(report.inventory_drift_abs_peak, 2))}</div></div>
  </div>

  <h2>Adverse selection (mean signed markout)</h2>
  <table>
    <thead><tr><th>Horizon</th><th>Mean</th><th>N</th></tr></thead>
    <tbody>{rows_markout}</tbody>
  </table>

  <h2>Inventory net (end)</h2>
  <table>
    <thead><tr><th>Market</th><th>Net shares</th></tr></thead>
    <tbody>{inv_rows}</tbody>
  </table>

  <h2>Reward accrual estimate (USDC)</h2>
  <table>
    <thead><tr><th>Market</th><th>Accrual</th></tr></thead>
    <tbody>{reward_rows}</tbody>
  </table>

  <h2>Markets seen</h2>
  <p class="sub">{_esc(", ".join(markets))}</p>
</main>
</body>
</html>
"""


def write_dashboard(metrics_log: Path, out: Path) -> MetricsReport:
    report = analyze(metrics_log)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard(report), encoding="utf-8")
    return report
