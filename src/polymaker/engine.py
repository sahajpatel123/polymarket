"""Engine: wires every component into a single async event loop.

Data flow per market:
  market WS -> OrderBook -> (wake) -> Quoter task -> strategy (pure) -> reconcile
  -> ExecutionGateway ; user WS -> StateStore ; periodic REST reconcile + heartbeat.

One lightweight quoter task per market, woken by book/fill events and debounced.
The strategy layer is pure; the engine owns all the state and I/O around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from polymaker.alerts import (
    API_AUTH,
    DAILY_LOSS,
    KILL_SWITCH,
    PROCESS_CRASH,
    WS_DISCONNECT,
    Alerter,
)
from polymaker.catalog.gamma import GammaClient, fetch_reward_rates, parse_market
from polymaker.catalog.scoring import score_market
from polymaker.catalog.store import CatalogStore
from polymaker.config import Config, StrategyProfile
from polymaker.domain import Fill, MarketMeta, Regime, Side
from polymaker.execution.gateway import ExecutionGateway
from polymaker.execution.reconciler import reconcile
from polymaker.journal import Journal
from polymaker.logging import get_logger
from polymaker.marketdata.orderbook import BookView
from polymaker.marketdata.parse import TradePrint
from polymaker.marketdata.service import MarketDataService
from polymaker.merge import Merger
from polymaker.metrics import MetricsLogger, inventory_fields
from polymaker.paper.fill_sim import FillSimulator
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore
from polymaker.state.tracker import UserEventProcessor
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.quoting import QuoteInputs, compute_fair_value, construct_quotes
from polymaker.strategy.regime import RegimeInputs, RegimeMachine
from polymaker.userstream.client import UserStream

log = get_logger("engine")


class Engine:
    def __init__(self, cfg: Config, *, paper: bool = False) -> None:
        self.cfg = cfg
        self.paper = paper
        self._running = False

        self.journal = Journal(cfg.paths.journal_dir, enabled=cfg.engine.journal,
                               day="paper" if paper else "live")
        metrics_name = "metrics-paper.jsonl" if paper else "metrics-live.jsonl"
        self.metrics = MetricsLogger(Path(cfg.paths.log_dir) / metrics_name, enabled=True)
        self.state = StateStore(cfg.paths.db)
        self.catalog = CatalogStore(cfg.paths.db)
        self.gateway = ExecutionGateway(cfg, self.journal, paper=paper)
        self.alerter = Alerter(cfg.secrets.alert_webhook_url, proxy=cfg.proxy)
        self.risk = RiskManager(cfg.risk, self.state)
        self.merger = Merger(cfg)

        self.md = MarketDataService(on_dirty=self._on_dirty, on_trade=self._on_trade,
                                    journal=self.journal, proxy=cfg.proxy)
        self.user_proc = UserEventProcessor(self.state, on_change=self._wake_cid,
                                            on_fill=self._on_fill)
        self.user: UserStream | None = None

        # per-market state
        self.metas: dict[str, MarketMeta] = {}
        self.profiles: dict[str, StrategyProfile] = {}
        self.est: dict[str, MarketEstimators] = {}
        self.regime_m: dict[str, RegimeMachine] = {}
        self._dirty: dict[str, asyncio.Event] = {}
        self._sweep: dict[str, bool] = {}
        self._merging: set[str] = set()
        self._token_cid: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}  # per-market: serialize recompute vs reconcile
        self._halted: set[str] = set()  # markets closed/resolved/not-accepting
        # supervised tasks: name -> (factory, task) so a dead task restarts
        self._task_specs: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._aux_tasks: list[asyncio.Task[Any]] = []  # fire-and-forget (merges)
        # health / recovery signals
        self._reconcile_now = asyncio.Event()
        self._user_started = False  # user WS task launched (live mode)
        self._hb_was_down = False
        self._chain_lock = asyncio.Lock()  # serialize on-chain txs (nonce safety)
        # paper-mode fill simulation: matches resting orders against trade prints
        # so paper mode can track inventory, PnL, and toxicity (live mode uses
        # the user WS for real fills).
        self._fill_sim = FillSimulator()

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        try:
            await self.gateway.connect()
        except Exception as exc:  # noqa: BLE001
            self.alerter.alert(API_AUTH, f"gateway connect/auth failed: {exc}", critical=True)
            raise
        await self._resolve_markets()
        if not self.metas:
            log.warning("no_markets_selected", hint="add markets to config/markets.toml, run `polymaker scan`")
        # freshen reward/fee/end-date params from live Gamma BEFORE quoting so a
        # stale catalog (e.g. old reward min-size) can't mis-size our orders
        await self.refresh_market_metadata()
        await self._startup_reconcile()

        # subscribe feeds
        self.md.set_markets([(cid, [m.yes.token_id, m.no.token_id]) for cid, m in self.metas.items()])
        self.user = UserStream(
            self.gateway.creds, self.gateway.address, self.user_proc,
            other_token=self._other_token, condition_of_token=self._cid_of_token,
            journal=self.journal, proxy=self.cfg.proxy,
            on_reconnect=self._on_user_reconnect,
        )
        self.user.set_markets(list(self.metas))

        # launch supervised tasks (a dead task is restarted, never silently gone)
        self._spawn("market_ws", self.md.run)
        if not self.paper:
            assert self.user is not None
            self._spawn("user_ws", self.user.run)
            # register the dead-man switch BEFORE any quoter can place an order,
            # so a crash between placing and the first heartbeat still auto-cancels
            with contextlib.suppress(Exception):
                await self.gateway.heartbeat()
            self._spawn("heartbeat", self._heartbeat_loop)
            self._user_started = True
        self._spawn("reconcile", self._reconcile_loop)
        self._spawn("metadata", self._metadata_refresh_loop)
        self._spawn("maintenance", self._maintenance_loop)
        for cid in self.metas:
            self._spawn(f"quote:{cid[:8]}", lambda c=cid: self._quoter(c))
        # Auto-discovery: periodically scan Gamma for new markets and
        # auto-add them to the live trade list. Off by default.
        if self.cfg.engine.auto_discovery_enabled:
            self._spawn("discovery", self._market_discovery_loop)
        # Hot-reload: watch markets.toml for manual edits.
        if self.cfg.engine.auto_discovery_hot_reload:
            self._spawn("hot_reload", self._hot_reload_loop)
        self._spawn("supervisor", self._supervise)
        self.risk.reset_day()
        log.info(
            "engine_started",
            markets=len(self.metas),
            paper=self.paper,
            auto_discovery=self.cfg.engine.auto_discovery_enabled,
            hot_reload=self.cfg.engine.auto_discovery_hot_reload,
        )

    def _spawn(self, name: str, factory: Any) -> None:
        self._task_specs[name] = factory
        self._tasks[name] = asyncio.create_task(factory(), name=name)

    _supervise_interval_s: float = 5.0

    async def _supervise(self) -> None:
        """Restart any engine task that exits while we're running. Never down."""
        while self._running:
            await asyncio.sleep(self._supervise_interval_s)
            for name, task in list(self._tasks.items()):
                if name == "supervisor" or not task.done():
                    continue
                if not self._running:
                    return
                exc = None
                with contextlib.suppress(asyncio.CancelledError, asyncio.InvalidStateError):
                    exc = task.exception()
                log.critical("task_died_restarting", task=name, err=str(exc) if exc else "exited")
                self.alerter.alert(
                    PROCESS_CRASH, f"{name} died: {exc}", critical=True
                )
                self._tasks[name] = asyncio.create_task(self._task_specs[name](), name=name)

    async def run_forever(self) -> None:
        await self.start()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self._tasks.values(), *self._aux_tasks)

    async def shutdown(self) -> None:
        self._running = False
        log.info("engine_shutdown")
        self.md.stop()
        if self.user:
            self.user.stop()
        for t in [*self._tasks.values(), *self._aux_tasks]:
            t.cancel()
        with contextlib.suppress(Exception):
            await self.gateway.cancel_all()
        self.gateway.close()
        self.journal.close()
        self.metrics.close()
        self.state.close()
        self.catalog.close()

    # ── market resolution ───────────────────────────────────────────────
    async def _resolve_markets(self) -> None:
        reward_rates: dict[str, float] | None = None
        async with GammaClient(self.cfg.wallet.gamma_host) as gamma:
            for entry in self.cfg.enabled_markets:
                meta = self.catalog.get_by_slug(entry.slug) if entry.slug else None
                if meta is None and entry.condition_id:
                    meta = self.catalog.get(entry.condition_id)
                if meta is None:  # fall back to a live Gamma fetch
                    if reward_rates is None:
                        reward_rates = await fetch_reward_rates(self.cfg.wallet.clob_host)
                    meta = await self._fetch_meta(gamma, entry.slug, entry.condition_id, reward_rates)
                if meta is None:
                    log.warning("market_unresolved", ref=entry.ref)
                    continue
                self.metas[meta.condition_id] = meta
                self.profiles[meta.condition_id] = self.cfg.profile_for(entry)
                self.est[meta.condition_id] = self._make_estimators(self.profiles[meta.condition_id])
                self.regime_m[meta.condition_id] = RegimeMachine()
                self._dirty[meta.condition_id] = asyncio.Event()
                self._locks[meta.condition_id] = asyncio.Lock()
                for tok in (meta.yes.token_id, meta.no.token_id):
                    self._token_cid[tok] = meta.condition_id

                sc = score_market(meta)
                self.metrics.emit(
                    "market_meta",
                    condition_id=meta.condition_id,
                    slug=meta.slug,
                    tick_size=meta.tick_size,
                    rewards_daily_rate=meta.rewards_daily_rate,
                    rewards_min_size=meta.rewards_min_size,
                    rewards_max_spread=meta.rewards_max_spread,
                    rebate_rate=meta.rebate_rate,
                    rebate_potential_daily=sc.rebate_potential,
                    score=sc.score,
                    taker_fee_bps=meta.taker_fee_bps,
                    fees_enabled=meta.fees_enabled,
                    paper=self.paper,
                )

    async def _fetch_meta(
        self, gamma: GammaClient, slug: str | None, condition_id: str | None,
        reward_rates: dict[str, float],
    ) -> MarketMeta | None:
        tag_id = self.catalog.cached_tag("politics")
        if tag_id is None:  # cold start: resolve + cache so the sweep is scoped
            tag_id = await gamma.resolve_tag_id("politics")
            if tag_id:
                self.catalog.cache_tag("politics", tag_id)
        async for raw in gamma.iter_markets(tag_id=tag_id, max_pages=25):
            if (slug and raw.get("slug") == slug) or (condition_id and raw.get("conditionId") == condition_id):
                m = parse_market(raw, reward_rates)
                if m:
                    self.catalog.upsert_market(m)
                return m
        return None

    @staticmethod
    def _make_estimators(p: StrategyProfile) -> MarketEstimators:
        return MarketEstimators(
            vol=VolEstimator(p.vol_short_halflife_s, p.vol_long_halflife_s),
            flow=FlowEstimator(p.flow_ewma_halflife_s),
            markout=MarkoutTracker(),
        )

    # ── dynamic market management (auto-discovery / hot-reload) ──────────
    async def add_market(self, meta: MarketMeta, profile: StrategyProfile) -> bool:
        """Dynamically add a market to the live trade list.

        Idempotent: if the market is already tracked, returns False. Otherwise:
        - Registers per-market state (estimators, regime, lock, dirty event)
        - Subscribes the WebSocket data service to its tokens
        - Adds it to the user-stream market set (live mode)
        - Spawns a supervised quoter task
        - Emits a market_meta metric for the metrics log
        Returns True on a fresh add, False if it was already tracked.
        """
        cid = meta.condition_id
        if cid in self.metas:
            return False
        self.metas[cid] = meta
        self.profiles[cid] = profile
        self.est[cid] = self._make_estimators(profile)
        self.regime_m[cid] = RegimeMachine()
        self._dirty[cid] = asyncio.Event()
        self._locks[cid] = asyncio.Lock()
        for tok in (meta.yes.token_id, meta.no.token_id):
            self._token_cid[tok] = cid

        # Subscribe the market data service to the new market's tokens
        self.md.add_market(cid, [meta.yes.token_id, meta.no.token_id])
        # Live mode: also tell the user stream which markets we now track
        if self.user is not None:
            self.user.set_markets(list(self.metas))

        sc = score_market(meta)
        self.metrics.emit(
            "market_meta",
            condition_id=cid,
            slug=meta.slug,
            tick_size=meta.tick_size,
            rewards_daily_rate=meta.rewards_daily_rate,
            rewards_min_size=meta.rewards_min_size,
            rewards_max_spread=meta.rewards_max_spread,
            rebate_rate=meta.rebate_rate,
            rebate_potential_daily=sc.rebate_potential,
            score=sc.score,
            taker_fee_bps=meta.taker_fee_bps,
            fees_enabled=meta.fees_enabled,
            paper=self.paper,
            auto_discovered=True,
        )
        # Spawn a supervised quoter for the new market
        self._spawn(f"quote:{cid[:8]}", lambda c=cid: self._quoter(c))
        log.info("market_added", condition_id=cid, slug=meta.slug, auto=True)
        return True

    async def remove_market(self, cid: str) -> bool:
        """Dynamically remove a market from the live trade list.

        Cancels any open orders on its tokens, removes per-market state, and
        stops the quoter task. Idempotent: returns False if not tracked.
        """
        if cid not in self.metas:
            return False
        meta = self.metas[cid]
        # Cancel any open orders on this market's tokens
        for tok in (meta.yes.token_id, meta.no.token_id):
            with contextlib.suppress(Exception):
                await self.gateway.cancel_asset(tok)
            for o in self.state.orders_for(tok):
                self.state.remove_order(o.order_id)
        # Cancel the quoter task
        task = self._tasks.pop(f"quote:{cid[:8]}", None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        # Remove from per-market state
        self.metas.pop(cid, None)
        self.profiles.pop(cid, None)
        self.est.pop(cid, None)
        self.regime_m.pop(cid, None)
        self._dirty.pop(cid, None)
        self._locks.pop(cid, None)
        self._halted.discard(cid)
        for tok in (meta.yes.token_id, meta.no.token_id):
            self._token_cid.pop(tok, None)
        # Tell the market data service to drop the subscription
        self.md.remove_market(cid)
        if self.user is not None:
            self.user.set_markets(list(self.metas))
        log.info("market_removed", condition_id=cid, slug=meta.slug)
        return True

    async def _market_discovery_loop(self) -> None:
        """Periodically scan Gamma for new markets and add them if they pass filters.

        Runs every `auto_discovery_interval_s`. Discovers markets across all
        configured tag categories, filters by minimum score, and caps at
        `auto_discovery_max_markets`. Also checks the metadata of already-
        tracked markets and removes closed/not-accepting ones.
        """
        if not self.cfg.engine.auto_discovery_enabled:
            return
        interval = max(60.0, float(self.cfg.engine.auto_discovery_interval_s))
        min_score = float(self.cfg.engine.auto_discovery_min_score)
        max_markets = int(self.cfg.engine.auto_discovery_max_markets)
        tags = tuple(self.cfg.engine.auto_discovery_tags) or ("politics",)
        profile_name = self.cfg.engine.auto_discovery_profile

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._run_discovery_pass(tags, min_score, max_markets, profile_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery_pass_failed", err=str(exc))

    async def _run_discovery_pass(
        self, tags: tuple[str, ...], min_score: float, max_markets: int, profile_name: str
    ) -> None:
        """One pass of market discovery: scan, score, add new markets, remove closed."""
        if profile_name not in self.cfg.profiles:
            log.warning("unknown_auto_discovery_profile", profile=profile_name)
            return
        profile = self.cfg.profiles[profile_name]
        from polymaker.catalog.scanner import ScanConfig, run_scan

        cfg = ScanConfig(
            tag_slugs=tags,
            min_liquidity=1000.0,
            rewards_only=True,
        )
        try:
            scanned = await run_scan(self.catalog, cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("discovery_scan_failed", err=str(exc))
            return

        # Score and filter
        candidates: list[MarketMeta] = []
        for meta in scanned:
            sc = score_market(meta)
            if sc.score < min_score:
                continue
            candidates.append(meta)

        # Add new markets (cap at max_markets total auto-discovered)
        auto_count = sum(
            1 for cid in self.metas
            if cid not in {e.condition_id for e in self.cfg.enabled_markets if e.condition_id}
        )
        added = 0
        for meta in candidates:
            if meta.condition_id in self.metas:
                continue
            if auto_count + added >= max_markets:
                break
            # Refresh metadata (rewards/fee may have changed since the scan)
            await self._apply_meta_to_market(meta)
            ok = await self.add_market(meta, profile)
            if ok:
                added += 1
                log.info(
                    "auto_market_added",
                    condition_id=meta.condition_id,
                    slug=meta.slug,
                    score=round(score_market(meta).score, 4),
                )

        # Remove markets that are no longer accepting orders (closed/resolved)
        await self._prune_closed_markets()

    async def _apply_meta_to_market(self, meta: MarketMeta) -> None:
        """Refresh a market's reward/fee params from the latest scan result."""
        if meta.condition_id in self.metas:
            old = self.metas[meta.condition_id]
            self.metas[meta.condition_id] = dataclasses.replace(
                old,
                rewards_min_size=meta.rewards_min_size,
                rewards_max_spread=meta.rewards_max_spread,
                rewards_daily_rate=meta.rewards_daily_rate,
                taker_fee_bps=meta.taker_fee_bps,
                rebate_rate=meta.rebate_rate,
                min_order_size=meta.min_order_size,
            )

    async def _prune_closed_markets(self) -> None:
        """Remove markets that Gamma now reports as closed or not-accepting."""
        if not self.metas:
            return
        try:
            from polymaker.catalog.gamma import GammaClient

            cids = list(self.metas.keys())
            async with GammaClient(self.cfg.wallet.gamma_host) as gamma:
                raws = await gamma.markets_by_condition(cids)
        except Exception as exc:  # noqa: BLE001
            log.warning("prune_scan_failed", err=str(exc))
            return

        for cid, raw in raws.items():
            if cid not in self.metas:
                continue
            accepting = bool(raw.get("acceptingOrders", True))
            closed = bool(raw.get("closed", False))
            if closed or not accepting:
                log.info("auto_market_removed_closed", condition_id=cid)
                await self.remove_market(cid)

    async def _hot_reload_loop(self) -> None:
        """Watch markets.toml for manual edits and reconcile with the live trade list.

        When the file changes, we reload it and:
        - Add any markets listed in the file that the engine doesn't track yet
        - Remove any markets the engine tracks that were removed from the file
        Manual edits to markets.toml take effect on the next file change event.
        """
        try:
            from watchfiles import awatch
        except ImportError:
            log.warning("hot_reload_disabled", reason="watchfiles not installed")
            return
        markets_path = self.cfg.config_dir / "markets.toml"
        if not markets_path.exists():
            log.warning("hot_reload_no_file", path=str(markets_path))
            return
        log.info("hot_reload_watching", path=str(markets_path))
        async for changes in awatch(str(markets_path)):
            for _change_type, path in changes:
                if not path.endswith("markets.toml"):
                    continue
                log.info("hot_reload_detected", path=path)
                # Debounce: small sleep to let the writer finish
                await asyncio.sleep(0.5)
                try:
                    await self._reconcile_market_list()
                except Exception as exc:  # noqa: BLE001
                    log.warning("hot_reload_failed", err=str(exc))
                break  # one pass per debounce window

    async def _reconcile_market_list(self) -> None:
        """Reconcile the engine's tracked markets with markets.toml.

        Adds any markets in the file that the engine doesn't yet track,
        and removes any tracked markets that were dropped from the file.
        Manual edits to markets.toml take effect here.
        """
        from polymaker.config import Config, MarketEntry

        fresh = Config.load(str(self.cfg.config_dir), load_env=False)
        desired: dict[str, MarketEntry] = {
            e.condition_id: e for e in fresh.enabled_markets if e.condition_id
        }
        # Current tracked CIDs that came from markets.toml (not auto-discovered)
        current_toml_cids = {
            e.condition_id for e in self.cfg.enabled_markets if e.condition_id
        }

        # Remove markets that were dropped from markets.toml
        for cid in list(current_toml_cids - set(desired.keys())):
            await self.remove_market(cid)
            log.info("hot_reload_removed", condition_id=cid)

        # Add markets that were added to markets.toml
        for entry in fresh.enabled_markets:
            cid = entry.condition_id
            if not cid or cid in self.metas:
                continue
            # Try to resolve the market
            meta = self.catalog.get(cid)
            if meta is None and entry.slug:
                meta = self.catalog.get_by_slug(entry.slug)
            if meta is None:
                # Fetch from Gamma
                try:
                    from polymaker.catalog.gamma import GammaClient, fetch_reward_rates, parse_market  # noqa: I001
                except ImportError:
                    pass
                else:
                    try:
                        async with GammaClient(self.cfg.wallet.gamma_host) as gamma:
                            reward_rates = await fetch_reward_rates(
                                self.cfg.wallet.clob_host
                            )
                            async for raw in gamma.iter_markets(
                                tag_id=None, max_pages=50
                            ):
                                if (
                                    (entry.slug and raw.get("slug") == entry.slug)
                                    or (cid and raw.get("conditionId") == cid)
                                ):
                                    meta = parse_market(raw, reward_rates)
                                    if meta:
                                        self.catalog.upsert_market(meta)
                                    break
                    except Exception as exc:  # noqa: BLE001
                        log.warning("hot_reload_fetch_failed", err=str(exc), ref=entry.ref)
            if meta is not None:
                profile = fresh.profile_for(entry)
                await self.add_market(meta, profile)
                log.info("hot_reload_added", condition_id=cid, slug=meta.slug)

    async def _startup_reconcile(self) -> None:
        with contextlib.suppress(Exception):
            await self.gateway.cancel_all()  # clean slate; heartbeat covers crashes
        # cancel-all may have partially failed — verify no orders remain, and
        # cancel/adopt any stragglers so we never quote on top of an unknown order
        with contextlib.suppress(Exception):
            leftover = await self.gateway.open_orders()
            if leftover:
                log.warning("startup_orders_remain", n=len(leftover))
                for tok in {o.token_id for o in leftover}:
                    await self.gateway.cancel_asset(tok)
                still = await self.gateway.open_orders()
                for tok in self._token_cid:
                    self.state.replace_open_orders(
                        tok, [o for o in still if o.token_id == tok], grace_s=0.0
                    )
                if still:
                    log.error("startup_orders_stuck", n=len(still))
                    self.alerter.alert("startup_orders_stuck",
                                       f"{len(still)} orders survived cancel-all", critical=True)
        # purge positions that leaked in for markets we don't trade (manual UI
        # bets etc.) so they can't distort exposure caps or PnL
        self.state.drop_untracked_positions(set(self._token_cid))
        self._fill_sim.clear()
        positions = self._only_traded(await self.gateway.positions())
        if positions:
            self.state.reconcile_positions(positions)
            log.info("startup_positions", n=len(positions))

    def _only_traded(self, positions: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
        """Scope account positions to tokens WE trade. Manual/UI positions in
        other markets are the operator's business — they must not enter our
        state, exposure caps, or PnL."""
        return {t: v for t, v in positions.items() if t in self._token_cid}

    # ── callbacks ───────────────────────────────────────────────────────
    def _on_dirty(self, condition_id: str, token_id: str) -> None:
        ev = self._dirty.get(condition_id)
        if ev is not None:
            ev.set()

    def _wake_cid(self, condition_id: str) -> None:
        ev = self._dirty.get(condition_id)
        if ev is not None:
            ev.set()

    def _wake_all(self) -> None:
        for ev in self._dirty.values():
            ev.set()

    def _on_user_reconnect(self) -> None:
        """User WS reconnected: events during the gap were lost — force an
        immediate REST reconcile before trusting our state again."""
        log.warning("user_ws_reconnected_forcing_reconcile")
        self._reconcile_now.set()

    def _on_trade(self, tp: TradePrint) -> None:
        cid = self._token_cid.get(tp.asset_id)
        if cid is None:
            return
        p = self.profiles[cid]
        self.est[cid].flow.update(tp.aggressor, tp.size, tp.ts)

        # Paper-mode fill simulation: match the trade print against our resting
        # orders so we can track inventory, PnL, and toxicity without a user WS.
        if self.paper:
            self._simulate_fills(tp)

        # A trade only flags a SWEEP (-> pull quotes) if it's genuinely toxic:
        # large in absolute terms AND large relative to the resting depth it
        # consumed (i.e. it actually ate through the book). A big trade absorbed
        # by a deep book doesn't move the price and isn't toxic — for a liquid
        # market the FV-jump detector is the real event signal. event_sweep_mult
        # sets how many order-sizes big the print must be to even be considered.
        base = p.base_size_usdc / max(tp.price, 0.01)
        if tp.size < p.event_sweep_mult * base:
            return
        book = self.md.book(tp.asset_id)
        if book is None:
            return
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None:
            return
        # aggressor BUY lifts asks; SELL hits bids — measure the side it consumed
        if tp.aggressor is Side.BUY:
            consumed = book.depth_within(Side.SELL, ba.price, ba.price + 3 * book.tick_size)
        else:
            consumed = book.depth_within(Side.BUY, bb.price - 3 * book.tick_size, bb.price)
        if consumed > 0 and tp.size >= p.event_sweep_frac * consumed:
            self._sweep[cid] = True

    def _simulate_fills(self, tp: TradePrint) -> None:
        """Match a trade print against paper-mode resting orders and process fills."""
        fills = self._fill_sim.match(tp.asset_id, tp.aggressor, tp.price, tp.size, tp.ts)
        if not fills:
            return
        cid = self._token_cid.get(tp.asset_id)
        if cid is None:
            return
        for fill in fills:
            if not self.state.apply_fill(fill):
                continue  # duplicate (shouldn't happen in paper, but be safe)
            self._on_fill(fill)
            # remove fully-filled orders from the simulator's tracking
            # (partial fills already reduced size; apply_fill updates state store)
            self._wake_cid(cid)

    def _on_fill(self, fill: Fill) -> None:
        self.risk.note_fill(fill)
        cid = self._token_cid.get(fill.token_id)
        if cid is None:
            return
        est = self.est[cid]
        fv = est.last_fv if est.last_fv is not None else fill.price
        meta = self.metas[cid]
        token_fv = fv if fill.token_id == meta.yes.token_id else (1.0 - fv)
        est.markout.record_fill(fill.side, token_fv, fill.ts)
        pos_yes = self.state.position(meta.yes.token_id)
        pos_no = self.state.position(meta.no.token_id)
        self.metrics.emit(
            "fill",
            ts=fill.ts,
            condition_id=cid,
            token_id=fill.token_id,
            side=fill.side.value,
            price=fill.price,
            size=fill.size,
            trade_id=fill.trade_id,
            mid=token_fv,
            fv=fv,
            paper=self.paper,
            **inventory_fields(pos_yes.size, pos_no.size),
        )

    # ── quoter ──────────────────────────────────────────────────────────
    async def _quoter(self, cid: str) -> None:
        debounce = self.cfg.engine.debounce_ms / 1000.0
        base_tick = self.cfg.engine.quoter_tick_s
        ev = self._dirty[cid]
        while self._running:
            try:
                # Book/fill events wake us instantly. Otherwise we refresh on a
                # slow baseline tick, EXCEPT: if an EVENT cool-off is active,
                # wake precisely when it ends (re-enter promptly, not up to a
                # minute late); if we're holding inventory, tick faster to walk
                # exit urgency.
                timeout = self._next_wake_s(cid, base_tick)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ev.wait(), timeout=timeout)
                if ev.is_set():
                    await asyncio.sleep(debounce)  # coalesce a burst of updates
                ev.clear()
                await self._recompute(cid)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.error("quoter_error", condition_id=cid, cid=cid[:8], err=str(exc))
                await asyncio.sleep(0.5)

    def _next_wake_s(self, cid: str, base_tick: float) -> float:
        now = time.time()
        wake = base_tick
        rm = self.regime_m.get(cid)
        if rm is not None:
            cd = rm.cooloff_remaining(now)
            if cd > 0:
                wake = min(wake, cd + 0.5)  # re-enter right when cool-off ends
        meta = self.metas.get(cid)
        if meta is not None:  # holding inventory -> tick faster to manage exits
            held = self.state.position(meta.yes.token_id).size + self.state.position(meta.no.token_id).size
            if held >= meta.min_order_size:
                wake = min(wake, 10.0)
        return max(1.0, wake)

    async def _recompute(self, cid: str) -> None:
        lock = self._locks.get(cid)
        if lock is None:
            return
        async with lock:  # serialize vs the reconcile loop mutating this market
            await self._recompute_locked(cid)

    async def _recompute_locked(self, cid: str) -> None:
        meta = self.metas[cid]
        p = self.profiles[cid]
        yes_book = self.md.book(meta.yes.token_id)
        no_book = self.md.book(meta.no.token_id)
        if yes_book is None or yes_book.is_empty:
            return

        # crossed/locked or one-sided book -> FV is unreliable; skip this tick.
        # Compute view once and reuse for the check + construct_quotes to avoid
        # redundant best_bid()/best_ask() calls on the hot path.
        yes_view = yes_book.view()
        if yes_view.best_bid is None or yes_view.best_ask is None:
            return
        if yes_view.best_bid >= yes_view.best_ask:
            return

        now = time.time()
        micro = yes_book.microprice(p.micro_levels)
        if micro is None:
            return
        est = self.est[cid]
        est.flow.decay_to(now)
        fv = compute_fair_value(micro, est.flow.z, meta.tick_size)
        prev_fv = est.last_fv
        est.on_fair_value(fv, now)

        self.risk.update_mark(meta.yes.token_id, fv)
        self.risk.update_mark(meta.no.token_id, 1.0 - fv)

        pos_yes = self.state.position(meta.yes.token_id)
        pos_no = self.state.position(meta.no.token_id)
        q_max = p.q_max_usdc
        inv_util = abs(pos_yes.size - pos_no.size) * fv / q_max if q_max > 0 else 0.0
        hours_to_end = _hours_to_end(meta.end_date_iso, now)

        # ── blind/stale conditions ──────────────────────────────────────────
        # A QUIET market with a live WS link is NOT stale — the CLOB WS pings
        # every 5s (pong-timeout 10s), so a dead link flips `connected` within
        # ~15s. Gating on the connection (not book-mutation recency) stops a
        # legitimately-quiet thin market from false-halting into zero rewards.
        market_stale = (
            not self.md.connected
            and self.md.disconnected_since > 0.0
            and (now - self.md.disconnected_since) > self.cfg.risk.ws_stale_halt_s
        )
        user_blind = (
            self._user_started
            and self.user is not None
            and not self.user.connected
            and (now - self.user.disconnected_since) > self.cfg.risk.user_ws_blind_halt_s
        )
        hb_blind = (
            not self.paper
            and self.cfg.engine.heartbeat
            and self.gateway.heartbeat_failures >= self.cfg.risk.heartbeat_halt_failures
        )
        halted = cid in self._halted
        blind = market_stale or user_blind or hb_blind or halted
        if blind:
            log.warning("market_blind", condition_id=cid, cid=cid[:8], market_stale=market_stale,
                        user_blind=user_blind, hb_blind=hb_blind, halted=halted)
            if market_stale or user_blind:
                self.alerter.alert(
                    WS_DISCONNECT,
                    f"{meta.question[:40]} ws disconnect "
                    f"(market_stale={market_stale} user_blind={user_blind})",
                    critical=True,
                )
            self.alerter.alert(
                f"blind:{cid[:8]}",
                f"{meta.question[:40]} blind (stale={market_stale} user={user_blind} "
                f"hb={hb_blind} halted={halted})",
                critical=hb_blind,
            )

        rd = self.risk.evaluate(meta, ws_stale=blind,
                                event_group_cost=self._event_group_cost(meta))
        if rd.halt and rd.reason not in ("ws_stale",):
            if "daily_loss" in rd.reason:
                self.alerter.alert(DAILY_LOSS, f"daily loss kill: {rd.reason}", critical=True)
            if "kill" in rd.reason:
                self.alerter.alert(KILL_SWITCH, f"kill switch: {rd.reason}", critical=True)
            self.alerter.alert(
                f"risk_halt:{rd.reason}", f"risk halt: {rd.reason}",
                critical=any(k in rd.reason for k in ("daily_loss", "kill", "error_rate")),
            )
        ws_stale = blind
        regime = self.regime_m[cid].decide(
            RegimeInputs(
                now=now, tick=meta.tick_size, fv=fv, prev_fv=prev_fv,
                vol_ratio=est.vol.ratio, flow_z=est.flow.z, inventory_util=inv_util,
                hours_to_end=hours_to_end, sweep_flagged=self._sweep.pop(cid, False),
                ws_stale=ws_stale, risk_halt=rd.halt, risk_reduce_only=rd.reduce_only,
            ),
            p,
        )

        tq = construct_quotes(QuoteInputs(
            meta=meta, regime=regime, fv=fv, vol_short=est.vol.short,
            toxicity=est.markout.toxicity, yes_view=yes_view,
            no_view=(no_book.view() if no_book else BookView(None, 0.0, None, 0.0, None, None, 0.0, 0.0)),
            pos_yes=pos_yes, pos_no=pos_no, profile=p, now=now,
            risk_size_scale=rd.size_scale,
        ))

        live = self.state.orders_for(meta.yes.token_id) + self.state.orders_for(meta.no.token_id)
        plan = reconcile(tq, live, tick=meta.tick_size,
                         reprice_ticks=p.reprice_ticks, resize_frac=p.resize_frac)

        inv = inventory_fields(pos_yes.size, pos_no.size)
        self.metrics.emit(
            "mark",
            ts=now,
            condition_id=cid,
            fv=fv,
            regime=regime.value,
            paper=self.paper,
            **inv,
        )

        if plan.is_noop:
            self._maybe_merge(cid, meta, p, pos_yes.size, pos_no.size)
            return

        if plan.to_cancel:
            pending_cancel = [self.state.orders[oid] for oid in plan.to_cancel if oid in self.state.orders]
            ok = await self.gateway.cancel(plan.to_cancel)
            if ok:
                for o in pending_cancel:
                    if self.paper:
                        self._fill_sim.cancel(o.order_id)
                    self.metrics.emit(
                        "cancel",
                        ts=now,
                        condition_id=cid,
                        token_id=o.token_id,
                        side=o.side.value,
                        price=o.price,
                        size=o.size,
                        order_id=o.order_id,
                        paper=self.paper,
                        **inv,
                    )
                for oid in plan.to_cancel:
                    self.state.remove_order(oid)
            else:
                # cancel MAY have partially applied server-side — keep our view,
                # resync from REST, and skip placing this cycle (avoid doubles)
                await self._refresh_token_orders(meta, grace_s=10.0)
                self._dirty[cid].set()
                return
        placed_n = 0
        if plan.to_place:
            # LOAD SHED: under rate-budget pressure, skip *new* quotes in calm
            # regimes (cancels/exits above already ran) so we don't inject latency
            # right when the book is busy. Risk regimes always place.
            shed = (
                not self.paper
                and self.gateway.order_pressure > 0.85
                and regime in (Regime.QUIET, Regime.TRENDING)
            )
            if shed:
                log.warning("shed_load", condition_id=cid, cid=cid[:8],
                            pressure=round(self.gateway.order_pressure, 2))
                self._dirty[cid].set()  # retry soon
            else:
                placed = await self.gateway.place(plan.to_place, meta)
                placed_n = len(placed)
                self.risk.note_order_result(len(placed) == len(plan.to_place))
                reward_band = meta.rewards_max_spread / 100.0
                for o in placed:
                    self.state.upsert_order(o)
                    if self.paper:
                        self._fill_sim.place(o)
                    mid_tok = fv if o.token_id == meta.yes.token_id else (1.0 - fv)
                    in_band = reward_band > 0 and abs(o.price - mid_tok) <= reward_band
                    self.metrics.emit(
                        "quote",
                        ts=now,
                        condition_id=cid,
                        token_id=o.token_id,
                        side=o.side.value,
                        price=o.price,
                        size=o.size,
                        order_id=o.order_id,
                        mid=mid_tok,
                        fv_yes=fv,
                        in_reward_band=in_band,
                        paper=self.paper,
                        **inv,
                    )
                if len(placed) < len(plan.to_place):
                    # QUARANTINE: a failed/partial batch may still have posted
                    # orders we don't have ids for. Cancel everything on these
                    # tokens (idempotent) and resync — never risk an untracked order.
                    await self._quarantine(meta, reason="place_incomplete")
        log.info("requote", condition_id=cid, cid=cid[:8], regime=regime.value, fv=round(fv, 4),
                 place=placed_n, cancel=len(plan.to_cancel),
                 pos_yes=round(pos_yes.size, 1), pos_no=round(pos_no.size, 1),
                 tox=round(est.markout.toxicity, 3), flowz=round(est.flow.z, 2),
                 vol_ratio=round(est.vol.ratio, 3))
        self._maybe_merge(cid, meta, p, pos_yes.size, pos_no.size)

    async def _quarantine(self, meta: MarketMeta, reason: str) -> None:
        """Cancel all orders on a market's tokens and resync state from REST."""
        log.warning("quarantine", condition_id=meta.condition_id, cid=meta.condition_id[:8],
                    reason=reason)
        for tok in (meta.yes.token_id, meta.no.token_id):
            await self.gateway.cancel_asset(tok)
            for o in self.state.orders_for(tok):
                self.state.remove_order(o.order_id)
        await self._refresh_token_orders(meta)

    async def _refresh_token_orders(self, meta: MarketMeta, grace_s: float = 0.0) -> None:
        """Open-orders resync for one market's tokens (grace_s=0 = authoritative)."""
        live = await self.gateway.open_orders()
        for tok in (meta.yes.token_id, meta.no.token_id):
            self.state.replace_open_orders(
                tok, [o for o in live if o.token_id == tok], grace_s=grace_s
            )

    def _maybe_merge(self, cid: str, meta: MarketMeta, p: StrategyProfile,
                     yes_size: float, no_size: float) -> None:
        amount = min(yes_size, no_size)
        if amount < p.merge_min_size or cid in self._merging or self.paper:
            return
        self._merging.add(cid)
        # Prune any previously completed merge tasks to prevent unbounded list growth.
        self._aux_tasks[:] = [t for t in self._aux_tasks if not t.done()]
        self._aux_tasks.append(asyncio.create_task(self._merge_task(cid, meta, amount)))

    async def _merge_task(self, cid: str, meta: MarketMeta, amount: float) -> None:
        try:
            # serialize all on-chain txs so concurrent merges can't reuse a nonce;
            # read on-chain balances as source of truth for the mergeable amount
            async with self._chain_lock:
                bals = await self.gateway.token_balances([meta.yes.token_id, meta.no.token_id])
                if bals:
                    amount = min(amount, bals.get(meta.yes.token_id, 0.0),
                                 bals.get(meta.no.token_id, 0.0))
                raw = int(amount * 1e6)
                if raw <= 0:
                    return
                await asyncio.to_thread(self.merger.merge, meta.condition_id, raw, meta.neg_risk)
        finally:
            self._merging.discard(cid)

    # ── background loops ────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        if not self.cfg.engine.heartbeat:
            return
        halt_after = self.cfg.risk.heartbeat_halt_failures
        while self._running:
            ok = await self.gateway.heartbeat()
            if not ok and self.gateway.heartbeat_failures >= halt_after and not self._hb_was_down:
                # exchange is (or soon will be) auto-cancelling everything we
                # have live; recompute will see hb_blind and pull quotes
                self._hb_was_down = True
                log.critical("heartbeat_down_halting", failures=self.gateway.heartbeat_failures)
                self._wake_all()
            elif ok and self._hb_was_down:
                # recovered: our server-side orders were wiped — drop local
                # order state, resync authoritatively, then resume quoting
                self._hb_was_down = False
                log.warning("heartbeat_recovered_resyncing")
                # Hold per-market locks during recovery so no quoter can race
                # between clear_orders() and the REST resync for its market.
                # Without this, a quoter could wake, see empty orders, place new
                # quotes, and then get overwritten by the stale REST snapshot.
                self.state.clear_orders()
                self._fill_sim.clear()
                for cid, meta in self.metas.items():
                    lock = self._locks.get(cid)
                    if lock is not None:
                        async with lock:
                            with contextlib.suppress(Exception):
                                await self._refresh_token_orders(meta, grace_s=0.0)
                    else:
                        with contextlib.suppress(Exception):
                            await self._refresh_token_orders(meta, grace_s=0.0)
                self._wake_all()
            await asyncio.sleep(self.cfg.engine.heartbeat_interval_s)

    async def _reconcile_loop(self) -> None:
        rounds = 0
        while self._running:
            # periodic cadence, but wake immediately when a reconnect/recovery
            # demands an urgent resync
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._reconcile_now.wait(),
                    timeout=self.cfg.engine.reconcile_interval_s,
                )
            forced = self._reconcile_now.is_set()
            self._reconcile_now.clear()
            rounds += 1
            try:
                # a MATCHED whose settlement event was lost would block a token's
                # reconciliation forever — expire stale in-flight guards first
                expired = self.state.expire_inflight(self.cfg.engine.reconcile_interval_s * 2)
                if expired:
                    self.alerter.alert("inflight_expired",
                                       f"{len(expired)} stuck in-flight guards cleared")

                positions = self._only_traded(await self.gateway.positions())
                if positions:
                    self.state.reconcile_positions(positions)
                live = await self.gateway.open_orders()
                by_token: dict[str, list[Any]] = {}
                for o in live:
                    by_token.setdefault(o.token_id, []).append(o)
                # iterate ALL our tokens, not just those in the REST response — a
                # token whose orders vanished server-side must be cleaned up too.
                # Hold the market lock so we don't race the quoter mid-flight.
                for cid, meta in self.metas.items():
                    lock = self._locks.get(cid)
                    if lock is None:
                        continue
                    async with lock:
                        for tok in (meta.yes.token_id, meta.no.token_id):
                            if self.state.inflight(tok) == 0:
                                self.state.replace_open_orders(tok, by_token.get(tok, []))
                if forced:
                    log.info("forced_reconcile_done", positions=len(positions),
                             open_orders=len(live))
                    self._wake_all()
            except Exception as exc:  # noqa: BLE001
                log.warning("reconcile_error", err=str(exc))

            # slower loops: on-chain position divergence + pnl snapshot + WAL
            if rounds % 4 == 0:
                with contextlib.suppress(Exception):
                    await self._check_position_divergence()
            self.state.record_pnl(self.risk.equity, self.risk.net_cash,
                                  self.risk.inventory_value, self.risk.daily_pnl)
            if rounds % 20 == 0:
                self.state.checkpoint_wal()

    async def _check_position_divergence(self) -> None:
        """Compare internal positions to on-chain truth; alert + correct on drift.

        Catches subtle fill-attribution bugs before they compound. On-chain is
        authoritative (it's what the exchange settles), so we correct to it —
        but only for tokens with no in-flight trades (optimistic state is newer).
        """
        tokens = [t for t in self._token_cid if self.state.inflight(t) == 0]
        onchain = await self.gateway.token_balances(tokens)
        if not onchain:
            return
        for tok, chain_size in onchain.items():
            internal = self.state.position(tok).size
            if abs(internal - chain_size) > max(1.0, 0.02 * chain_size):
                log.error("position_divergence", token=tok[:12],
                          internal=round(internal, 2), onchain=round(chain_size, 2))
                self.alerter.alert(
                    f"divergence:{tok[:8]}",
                    f"position drift: internal {internal:.1f} vs on-chain {chain_size:.1f}",
                    critical=True,
                )
                self.state.force_set_position(tok, chain_size, self.state.position(tok).avg_price,
                                              source="onchain")
                cid = self._token_cid.get(tok)
                if cid:
                    self._wake_cid(cid)

    async def refresh_market_metadata(self) -> None:
        """Pull fresh metadata from Gamma for all traded markets: halt on
        closed/not-accepting, and freshen reward/fee/end-date params so we quote
        at the CURRENT reward minimum, band, and fees (these change over time —
        e.g. the reward min-size jumping 50->100 shares). Called at startup and
        periodically. Safe to await."""
        if not self.metas:
            return
        try:
            async with GammaClient(self.cfg.wallet.gamma_host) as gamma:
                raws = await gamma.markets_by_condition(list(self.metas))
        except Exception as exc:  # noqa: BLE001
            log.warning("metadata_refresh_error", err=str(exc))
            return
        for cid, raw in raws.items():
            if cid not in self.metas:
                continue
            accepting = bool(raw.get("acceptingOrders", True))
            closed = bool(raw.get("closed", False))
            if closed or not accepting:
                if cid not in self._halted:
                    self._halted.add(cid)
                    log.critical("market_halted_by_meta", condition_id=cid, cid=cid[:8],
                                 closed=closed, accepting=accepting)
                    self.alerter.alert(f"halted:{cid[:8]}",
                                       f"{self.metas[cid].question[:40]} closed/not-accepting",
                                       critical=True)
                    meta = self.metas[cid]
                    for tok in (meta.yes.token_id, meta.no.token_id):
                        with contextlib.suppress(Exception):
                            await self.gateway.cancel_asset(tok)
                    self._wake_cid(cid)
                continue
            self._halted.discard(cid)
            self._apply_meta_refresh(cid, raw)

    def _apply_meta_refresh(self, cid: str, raw: dict[str, Any]) -> None:
        old = self.metas[cid]
        fee = raw.get("feeSchedule") or {}
        rate = _fnum(fee.get("rate"))
        candidates: dict[str, Any] = {
            "rewards_min_size": _fnum(raw.get("rewardsMinSize")),
            "rewards_max_spread": _fnum(raw.get("rewardsMaxSpread")),
            "taker_fee_bps": int(round(rate * 10000)) if rate is not None else None,
            "rebate_rate": _fnum(fee.get("rebateRate")),
            "end_date_iso": raw.get("endDate"),
            "min_order_size": _fnum(raw.get("orderMinSize")),
        }
        updates = {k: v for k, v in candidates.items()
                   if v is not None and getattr(old, k) != v}
        if updates:
            self.metas[cid] = dataclasses.replace(old, **updates)
            log.info("meta_refreshed", condition_id=cid, cid=cid[:8], **updates)
            self._wake_cid(cid)

    async def _metadata_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.cfg.engine.catalog_refresh_s)
            await self.refresh_market_metadata()

    async def _maintenance_loop(self) -> None:
        """Periodic REST book refresh to catch any silently-missed WS deltas."""
        while self._running:
            await asyncio.sleep(120.0)
            for meta in list(self.metas.values()):
                for tok in (meta.yes.token_id, meta.no.token_id):
                    with contextlib.suppress(Exception):
                        await self._refresh_book(tok)

    async def _refresh_book(self, token_id: str) -> None:
        levels = await self.gateway.get_full_book(token_id)
        if levels is None:
            return
        bids, asks, book_hash = levels
        book = self.md.book(token_id)
        if book is None:
            return
        # drift check: only overwrite if the REST top-of-book disagrees with ours
        cur_bb = book.best_bid()
        cur_ba = book.best_ask()
        rest_bb = max((p for p, _ in bids), default=None)
        rest_ba = min((p for p, _ in asks), default=None)
        drift = (
            (cur_bb is None) != (rest_bb is None)
            or (cur_ba is None) != (rest_ba is None)
            or (cur_bb and rest_bb and abs(cur_bb.price - rest_bb) > book.tick_size)
            or (cur_ba and rest_ba and abs(cur_ba.price - rest_ba) > book.tick_size)
        )
        if drift:
            log.warning("book_drift_corrected", token=token_id[:12])
            book.apply_snapshot(bids, asks, time.time(), book_hash)
            cid = self._token_cid.get(token_id)
            if cid:
                self._wake_cid(cid)

    # ── helpers ─────────────────────────────────────────────────────────
    def _other_token(self, token_id: str) -> str | None:
        cid = self._token_cid.get(token_id)
        return self.metas[cid].other_token(token_id) if cid else None

    def _cid_of_token(self, token_id: str) -> str | None:
        return self._token_cid.get(token_id)

    def engage_kill_switch(self, reason: str = "manual_kill") -> None:
        """Operator/manual kill — alerts then sets RiskManager killed flag.

        Does not change kill thresholds; only notifies + engages existing switch.
        """
        self.risk.kill()
        self.alerter.alert(KILL_SWITCH, f"kill switch engaged: {reason}", critical=True)
        self._wake_all()

    def _event_group_cost(self, meta: MarketMeta) -> float:
        if not meta.event_id:
            return 0.0
        cost = 0.0
        for m in self.metas.values():
            if m.event_id == meta.event_id:
                for tok in (m.yes.token_id, m.no.token_id):
                    pos = self.state.position(tok)
                    cost += pos.size * pos.avg_price
        return cost


def _fnum(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _hours_to_end(end_date_iso: str | None, now: float) -> float | None:
    if not end_date_iso:
        return None
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        hrs = (dt.timestamp() - now) / 3600.0
        # A past end date on a still-trading market is a stale/placeholder date
        # (common for "next X" appointment markets) — treat as unknown so we
        # don't wrongly HALT. The true end is signalled by acceptingOrders=False,
        # which the metadata refresh already halts on.
        return hrs if hrs > 0.0 else None
    except (ValueError, TypeError):
        return None

