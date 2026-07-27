"""polymaker command-line interface.

  polymaker scan                 sweep Gamma for political markets -> SQLite
  polymaker markets              rank/browse the catalog
  polymaker markets-add <slug>   append a market to config/markets.toml
  polymaker status               positions / open orders / PnL (reads SQLite)
  polymaker doctor               preflight: wallet auth, balances, WS reachability
  polymaker run [--paper]        start the market maker
  polymaker cancel-all           panic button
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from polymaker import __version__
from polymaker.config import Config

app = typer.Typer(
    name="polymaker",
    help="Maker-only market maker for Polymarket CLOB V2.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the polymaker version."""
    console.print(f"polymaker {__version__}")


@app.command()
def scan(
    config_dir: str = typer.Option("config", help="config directory"),
    min_liquidity: float = typer.Option(1000.0, help="minimum market liquidity (USDC)"),
    all_markets: bool = typer.Option(False, "--all", help="include non-rewards markets"),
    categories: str = typer.Option("politics", "--categories",
                                   help="comma-separated Gamma tag slugs to scan (e.g. 'politics,sports,crypto')"),
) -> None:
    """Sweep Gamma for markets across categories, score, and persist to SQLite."""
    from polymaker.catalog.scanner import ScanConfig, run_scan
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)

    tag_slugs = tuple(s.strip() for s in categories.split(",") if s.strip())

    async def _go() -> int:
        metas = await run_scan(store, ScanConfig(
            tag_slugs=tag_slugs,
            min_liquidity=min_liquidity,
            rewards_only=not all_markets,
        ))
        return len(metas)

    n = asyncio.run(_go())
    csv_path = Path(config_dir).parent / "markets.csv"
    written = store.export_csv(csv_path)
    console.print(f"[green]Scanned and stored {n} markets across {len(tag_slugs)} categor(y/ies): "
                  f"{', '.join(tag_slugs)}.[/green] "
                  f"Wrote [bold]{csv_path}[/bold] ({written} rows) — open it, pick markets, "
                  f"then `polymaker markets-add <slug>`.")
    store.close()


@app.command()
def markets(
    config_dir: str = typer.Option("config", help="config directory"),
    limit: int = typer.Option(25, help="rows to show"),
) -> None:
    """Show the top scored markets from the catalog."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    rows = store.top(limit)
    if not rows:
        console.print("[yellow]Catalog empty. Run `polymaker scan` first.[/yellow]")
        raise typer.Exit()

    table = Table(title="Political markets by score")
    for col in ("score", "reward/day", "rebate/day", "spread", "tick", "neg", "question"):
        table.add_column(col, justify="right" if col != "question" else "left")
    for meta, sc in rows:
        table.add_row(
            f"{sc.score:.2f}", f"{meta.rewards_daily_rate:.0f}", f"{sc.rebate_potential:.0f}",
            f"{sc.spread:.3f}", f"{meta.tick_size:g}", "Y" if meta.neg_risk else "-",
            meta.question[:60],
        )
    console.print(table)
    console.print("\nAdd one with: [bold]polymaker markets-add <slug>[/bold]  (slugs are in the catalog)")


@app.command(name="markets-add")
def markets_add(
    slug: str,
    profile: str = typer.Option("political-longdated", help="strategy profile"),
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """Append a market (by slug) to config/markets.toml."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    meta = store.get_by_slug(slug)
    store.close()
    if meta is None:
        console.print(f"[red]No market with slug {slug!r} in the catalog. Run `polymaker scan`.[/red]")
        raise typer.Exit(1)

    path = Path(config_dir) / "markets.toml"
    block = f'\n[[markets]]\nslug    = "{slug}"\nprofile = "{profile}"\nenabled = true\n'
    with path.open("a") as fh:
        fh.write(block)
    console.print(f"[green]Added[/green] {meta.question[:60]!r} to {path}")


@app.command()
def status(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show positions, open orders, and marks from the local state DB."""
    from polymaker.state.store import StateStore

    cfg = Config.load(config_dir)
    store = StateStore(cfg.paths.db)
    snap = store.snapshot()
    console.print(f"[bold]Open orders:[/bold] {snap['open_orders']}")
    positions: dict[str, Any] = snap["positions"]  # type: ignore[assignment]
    if not positions:
        console.print("[dim]No open positions.[/dim]")
    else:
        table = Table(title="Positions")
        table.add_column("token")
        table.add_column("size", justify="right")
        table.add_column("avg", justify="right")
        for tok, p in positions.items():
            table.add_row(tok[:16] + "…", f"{p['size']:.2f}", f"{p['avg_price']:.3f}")
        console.print(table)
    store.close()


@app.command()
def pnl(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show PnL from the recorded snapshots (equity, daily PnL, fills)."""
    import sqlite3

    cfg = Config.load(config_dir)
    conn = sqlite3.connect(cfg.paths.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, equity, net_cash, inventory_value, daily_pnl FROM pnl_snapshots "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchall()
    if not rows:
        console.print("[yellow]No PnL snapshots yet (run the engine first).[/yellow]")
    else:
        r = rows[0]
        color = "green" if r["daily_pnl"] >= 0 else "red"
        console.print(f"[bold]equity:[/bold] {r['equity']:.4f}  "
                      f"[bold]inventory:[/bold] {r['inventory_value']:.4f}  "
                      f"[bold]net cash:[/bold] {r['net_cash']:.4f}")
        console.print(f"[bold]daily PnL:[/bold] [{color}]{r['daily_pnl']:+.4f}[/{color}] pUSD")
    nfills = conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"]
    console.print(f"[dim]total fills recorded: {nfills}[/dim]")
    conn.close()


@app.command(name="export-csv")
def export_csv(
    config_dir: str = typer.Option("config", help="config directory"),
    out: str = typer.Option("markets.csv", help="output CSV path"),
    limit: int = typer.Option(500, help="max rows"),
) -> None:
    """Export the scored market catalog to a CSV for easy picking."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    n = store.export_csv(out, limit)
    store.close()
    console.print(f"[green]Wrote {n} markets to {out}.[/green]")


@app.command()
def dashboard(
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(True, "--paper/--live", help="which metrics log to render"),
    out: str = typer.Option("logs/dashboard.html", help="HTML output path"),
) -> None:
    """Render a local HTML metrics dashboard from the metrics JSONL log."""
    from polymaker.metrics.dashboard import write_dashboard

    cfg = Config.load(config_dir)
    log_name = "metrics-paper.jsonl" if paper else "metrics-live.jsonl"
    log_path = Path(cfg.paths.log_dir) / log_name
    out_path = Path(out)
    rep = write_dashboard(log_path, out_path)
    console.print(
        f"[green]Wrote[/green] {out_path}  "
        f"(quotes={rep.n_quote} fills={rep.n_fill} "
        f"spread={rep.realized_spread_usdc:.4f})"
    )


@app.command()
def run(
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(False, "--paper", help="paper mode: full pipeline, no orders posted"),
) -> None:
    """Start the market maker."""
    from polymaker.engine import Engine
    from polymaker.logging import configure

    cfg = Config.load(config_dir)
    configure(json_file=Path(cfg.paths.log_dir) / ("paper.jsonl" if paper else "live.jsonl"))
    if cfg.engine.loop == "uvloop":
        try:
            import uvloop

            uvloop.install()
        except Exception:  # noqa: BLE001
            pass

    engine = Engine(cfg, paper=paper)

    async def _go() -> None:
        try:
            await engine.run_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await engine.shutdown()

    console.print(f"[bold green]Starting polymaker[/bold green] ({'PAPER' if paper else 'LIVE'})…")
    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")
    except Exception as exc:  # noqa: BLE001
        from polymaker.alerts import PROCESS_CRASH

        engine.alerter.alert(PROCESS_CRASH, f"engine crashed: {exc}", critical=True)
        raise


@app.command()
def livetest(
    config_dir: str = typer.Option("config", help="config directory"),
    notional: float = typer.Option(5.0, help="order notional in USDC"),
) -> None:
    """Live wallet round-trip: place a deep post-only order and cancel it (~$5)."""
    from polymaker.livetest import run_livetest

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_livetest(cfg, console, notional))
    raise typer.Exit(0 if ok else 1)


@app.command()
def moneydoctor(
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """LIVE trading self-test: rest a limit, then market buy + sell (spends a little)."""
    from polymaker.moneydoctor import run_moneydoctor

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_moneydoctor(cfg, console))
    raise typer.Exit(0 if ok else 1)


@app.command(name="cancel-all")
def cancel_all(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Cancel all open orders for the wallet (panic button)."""
    from polymaker.execution.gateway import ExecutionGateway

    cfg = Config.load(config_dir)
    gw = ExecutionGateway(cfg)

    async def _go() -> None:
        await gw.connect()
        await gw.cancel_all()

    asyncio.run(_go())
    console.print("[green]Sent cancel-all.[/green]")


# ── V3: self-improve / review / explain / capital / memory ─────────────────


def _v3_db(config_dir: str) -> str:
    cfg = Config.load(config_dir)
    return cfg.paths.db


def _load_profile_dict(config_dir: str, name: str | None = None) -> dict[str, Any]:
    cfg = Config.load(config_dir)
    if name and name in cfg.profiles:
        return cfg.profiles[name].model_dump()
    if cfg.profiles:
        first = next(iter(cfg.profiles.values()))
        return first.model_dump()
    return {}


@app.command()
def improve(
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(False, "--paper", help="paper mode (standalone; no live posts)"),
    force: bool = typer.Option(
        True,
        "--force/--no-force",
        help="run even if metrics look healthy (default: true for standalone)",
    ),
    profile: str = typer.Option("", help="strategy profile name (default: first loaded)"),
) -> None:
    """Run one self-improvement cycle (standalone; engine need not be running)."""
    from polymaker.intelligence.profile_history import ProfileHistory
    from polymaker.intelligence.review import load_memory
    from polymaker.intelligence.self_eval import SelfEvaluation
    from polymaker.intelligence.self_improve import SelfImprover

    _ = paper  # paper/live both supported; cycle itself is offline+LLM
    db = _v3_db(config_dir)
    live = _load_profile_dict(config_dir, profile or None)
    hist = ProfileHistory(db)
    mem = load_memory(db)
    # Standalone: empty SelfEvaluation unless engine wired; --force drives LLM.
    ev = SelfEvaluation()
    improver = SelfImprover(history=hist, memory=mem, profile_name=profile or "default")
    improver.set_live_profile(live)
    try:
        result = improver.run(ev, force=force)
    except Exception as exc:
        console.print(f"[red]improve failed:[/red] {exc}")
        hist.close()
        raise typer.Exit(1) from exc

    console.print(f"[bold]triggered:[/bold] {result.triggered}  [bold]reason:[/bold] {result.reason}")
    if result.suggestion:
        console.print(f"[bold]diagnosis:[/bold] {result.suggestion.diagnosis}")
        console.print(f"[bold]suggestion:[/bold] {result.suggestion.suggestion}")
        console.print(
            f"[bold]expected_impact_pct:[/bold] {result.suggestion.expected_impact_pct}"
        )
        console.print(
            f"[bold]paper_validation_required:[/bold] "
            f"{result.suggestion.paper_validation_required}"
        )
    console.print(
        f"[bold]applied:[/bold] {result.applied}  "
        f"[bold]promoted:[/bold] {result.promoted}  "
        f"[bold]rejected:[/bold] {result.rejected}"
    )
    hist.close()


@app.command()
def review(
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(False, "--paper", help="paper mode (standalone)"),
    out_dir: str = typer.Option(
        "",
        help="override reviews directory (default: <config_dir>/daily_reviews)",
    ),
) -> None:
    """Run end-of-day review now (standalone; writes livecfg/daily_reviews/)."""
    from datetime import UTC, datetime

    from polymaker.intelligence.review import DaySummary, load_memory, run_daily_review

    _ = paper
    db = _v3_db(config_dir)
    mem = load_memory(db)
    # Best-effort PnL snapshot from SQLite when present.
    pnl = 0.0
    fills = 0
    try:
        import sqlite3

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT daily_pnl FROM pnl_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            pnl = float(row["daily_pnl"])
        try:
            fills = int(conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"])
        except Exception:
            fills = 0
        conn.close()
    except Exception:
        pass

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    summary = DaySummary(
        date_utc=day,
        pnl=pnl,
        fills=fills,
        memory_growth=len(mem.recent(100)),
    )
    reviews = out_dir or str(Path(config_dir) / "daily_reviews")
    try:
        result = run_daily_review(summary, memory=mem, reviews_dir=reviews)
    except Exception as exc:
        console.print(f"[red]review failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[bold]grade:[/bold] {result.grade}")
    console.print(f"[bold]report:[/bold] {result.report_path}")
    for i, p in enumerate(result.top_3_problems, 1):
        console.print(f"  problem {i}: {p}")
    for i, w in enumerate(result.top_3_wins, 1):
        console.print(f"  win {i}: {w}")


@app.command()
def explain(
    cid: str = typer.Argument(..., help="condition id (market) to explain"),
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(False, "--paper", help="paper mode (standalone)"),
) -> None:
    """LLM narrative for the current state of a market (standalone)."""
    from polymaker.intelligence.review import load_memory
    from polymaker.intelligence.self_improve import call_grok_reasoning

    _ = paper
    db = _v3_db(config_dir)
    mem = load_memory(db)
    # Local context: catalog row if present.
    question = ""
    try:
        from polymaker.catalog.store import CatalogStore

        store = CatalogStore(db)
        meta = store.get(cid)
        if meta is None:
            # try slug-less: scan by condition_id attribute if available
            for m, _sc in store.top(500):
                if m.condition_id == cid:
                    meta = m
                    break
        if meta is not None:
            question = meta.question
        store.close()
    except Exception:
        pass

    recent = [getattr(x, "text", str(x)) for x in mem.recent(5)]
    system = (
        "You are Polymaker's explainability layer. Return JSON with keys: "
        "narrative (str), regime_guess (str), risks (array of str)."
    )
    user = json.dumps(
        {"condition_id": cid, "question": question, "memory": recent},
        default=str,
    )
    try:
        data = call_grok_reasoning(system=system, user=user)
    except Exception as exc:
        console.print(f"[red]explain failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[bold]market:[/bold] {cid}")
    if question:
        console.print(f"[dim]{question}[/dim]")
    console.print(data.get("narrative", data))


@app.command()
def capital(
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(False, "--paper", help="paper mode"),
) -> None:
    """Show current capital allocation breakdown."""
    import os

    _ = paper
    capital_usdc = float(os.environ.get("POLYMAKER_CAPITAL_USDC", "0") or 0)
    console.print(f"[bold]POLYMAKER_CAPITAL_USDC:[/bold] {capital_usdc}")

    # Prefer Agent-3 orchestrator when present.
    try:
        from polymaker.intelligence.orchestrator import (  # type: ignore
            load_capital_usdc,
            plan_allocations,
        )

        cap = float(load_capital_usdc())
        console.print(f"[bold]resolved capital:[/bold] {cap}")
        try:
            plan = plan_allocations([])  # empty candidates → empty plan
            console.print(f"[bold]allocation plan:[/bold] {plan}")
        except TypeError:
            console.print(
                "[dim]orchestrator present; pass live candidates via engine for full plan.[/dim]"
            )
        return
    except Exception:
        pass

    # Fallback: show markets.toml weights equally.
    try:
        cfg = Config.load(config_dir)
        enabled = [m for m in cfg.markets if getattr(m, "enabled", True)]
        if not enabled:
            console.print("[yellow]No enabled markets; capital idle.[/yellow]")
            return
        each = capital_usdc / len(enabled) if capital_usdc > 0 else 0.0
        table = Table(title="Capital allocation (equal-weight fallback)")
        table.add_column("slug")
        table.add_column("profile")
        table.add_column("usdc", justify="right")
        for m in enabled:
            table.add_row(m.slug, m.profile, f"{each:.2f}")
        console.print(table)
    except Exception as exc:
        console.print(f"[yellow]capital view limited:[/yellow] {exc}")


memory_app = typer.Typer(
    name="memory",
    help="Show or search agent memory items.",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")


@memory_app.callback(invoke_without_command=True)
def memory_root(
    ctx: typer.Context,
    config_dir: str = typer.Option("config", help="config directory"),
    limit: int = typer.Option(20, help="rows to show"),
    paper: bool = typer.Option(False, "--paper", help="paper mode"),
) -> None:
    """Show recent memory items (default when no subcommand)."""
    if ctx.invoked_subcommand is not None:
        return
    _ = paper
    from polymaker.intelligence.review import load_memory

    mem = load_memory(_v3_db(config_dir))
    items = mem.recent(limit)
    if not items:
        console.print("[dim]No memory items yet.[/dim]")
        return
    for it in items:
        text = getattr(it, "text", str(it))
        ts = getattr(it, "ts", 0)
        console.print(f"[dim]{ts:.0f}[/dim]  {text}")


@memory_app.command("search")
def memory_search(
    q: str = typer.Argument(..., help="search query"),
    config_dir: str = typer.Option("config", help="config directory"),
    limit: int = typer.Option(20, help="max matches"),
    paper: bool = typer.Option(False, "--paper", help="paper mode"),
) -> None:
    """Search memory for a query string."""
    _ = paper
    from polymaker.intelligence.review import load_memory

    mem = load_memory(_v3_db(config_dir))
    items = mem.search(q, limit=limit)
    if not items:
        console.print(f"[dim]No memory matches for {q!r}.[/dim]")
        return
    for it in items:
        text = getattr(it, "text", str(it))
        console.print(text)


if __name__ == "__main__":
    app()
