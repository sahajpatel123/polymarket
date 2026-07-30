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
from polymaker.benchmark.capital import (
    MakerRewardEligibility,
    decide_maker_reward_eligibility,
)
from polymaker.catalog.gamma import GammaClient, fetch_reward_rates, parse_market
from polymaker.catalog.scoring import score_market
from polymaker.catalog.store import CatalogStore
from polymaker.config import Config, StrategyProfile
from polymaker.domain import Fill, MarketMeta, Regime, Side
from polymaker.execution.gateway import ExecutionGateway
from polymaker.execution.reconciler import reconcile
from polymaker.intelligence import (
    DecisionFramework,
    GovernedDeepSeekAgent,
    DeepSeekAgent,
    LLMGovernance,
    MarketDiscovery,
    OversightLoop,
)
from polymaker.intelligence.memory import AgentMemory
from polymaker.intelligence.policy import load_capital_usdc
from polymaker.intelligence.profile_history import ProfileHistory
from polymaker.intelligence.self_improve import SelfImprover
from polymaker.journal import Journal
from polymaker.logging import get_logger
from polymaker.marketdata.orderbook import BookView
from polymaker.marketdata.parse import TradePrint
from polymaker.marketdata.service import MarketDataService
from polymaker.merge import Merger
from polymaker.metrics import MetricsLogger, inventory_fields
from polymaker.paper.fill_sim import FillSimulator
from polymaker.risk.degradation import DegradationDetector
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore
from polymaker.state.tracker import UserEventProcessor
from polymaker.strategy.decision_pipeline import build_targets
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MultiHorizonMarkout,
    VolEstimator,
)
from polymaker.strategy.quoting import compute_fair_value
from polymaker.strategy.regime import RegimeMachine
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
        self._halted: set[str] = set()  # markets closed/resolved/not-accepting (Gamma)
        # Ops/LLM pause — independent of Gamma; must NOT be cleared by metadata refresh
        self._llm_paused: set[str] = set()
        self._pending_pause_cancels: set[str] = set()
        # condition_id → last requote regime (for live dashboard Book layout)
        self._last_regime: dict[str, str] = {}
        # condition_id → last capital/reward eligibility decision (skip vs floor)
        self._reward_eligibility: dict[str, MakerRewardEligibility] = {}
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
        # discovery capital allocation (cid → USDC) from allocate_capital
        self._discovery_capital: dict[str, float] = {}
        # Intelligence / trade-judgment brain (pure; fed from book/trade/fills)
        self.intel = DecisionFramework(max_active_markets=0)
        # Degradation: auto retreat when markout/drawdown collapses
        self.degradation = DegradationDetector()
        # Quarantined markets: REDUCE_ONLY (exits allowed), never HALTED
        self._quarantined: set[str] = set()
        # Per-market trade timestamps for dead/stale detection (last hour)
        self._trade_ts: dict[str, list[float]] = {}
        self._last_book_ts: dict[str, float] = {}
        # Inventory entry timestamps for exit urgency (token_id -> first open ts)
        self._pos_entry_ts: dict[str, float] = {}
        # LLM / V3 governance: wired only when DEEPSEEK_API_KEY is in .env
        self.deepseek_agent: DeepSeekAgent | None = None
        self.gov_agent: GovernedDeepSeekAgent | None = None
        self.oversight_loop: OversightLoop | None = None
        self.llm_gov: LLMGovernance | None = None
        self._llm_actions: list = []
        self._llm_enabled = bool(cfg.secrets.deepseek_api_key)
        self._per_market_spread_mult: dict[str, float] = {}
        # ── DeepSeek per-market trading authority ─────────────────
        # DeepSeek sets these via oversigt actions; engine reads them
        # on every requote. DeepSeek = sizing + aggression authority.
        self._grok_aggression: dict[str, float] = {}        # 0.5-2.0 (1.0 = normal)
        self._grok_band_override: dict[str, float] = {}      # 0.2-0.8
        # ─── DeepSeek automated triggers (0 API cost, sub-second evaluation) ──
        from polymaker.intelligence.deepseek_triggers import DeepSeekTrigger
        self._deepseek_triggers: list[DeepSeekTrigger] = []
        # ────────────────────────────────────────────────────────────────
        # ─────────────────────────────────────────────────────
        # V3: long-term memory + self-improve + review
        self.memory: AgentMemory | None = None
        self.profile_hist: ProfileHistory | None = None
        self.self_improver: SelfImprover | None = None
        self._last_improve_ts: float = 0.0
        self._last_review_ts: float = 0.0
        self._discovery_agent: MarketDiscovery | None = None
        # LLM-ranked selection (last discovery rankings + governed facade)
        self._llm_rankings: list[Any] = []
        self._gov_facade: Any | None = None
        # Auto-compounding: effective bankroll tracks PnL growth
        self._base_capital: float = (
            load_capital_usdc() or float(cfg.risk.bankroll_usdc or 0)
        )
        self._day_start_equity: float = 0.0
        self._effective_capital: float = self._base_capital

    def wire_llm_stack(
        self,
        *,
        agent: Any | None = None,
        force_capital_usdc: float | None = None,
    ) -> bool:
        """Wire governed DeepSeek + oversight + discovery on the live/paper path.

        Called from :meth:`start`. Extracted so unit tests can drive the
        **shipped** wiring with a mock agent (no network).

        Returns True when LLM stack is active. Conditions:
        - ``DEEPSEEK_API_KEY`` present (or a mock ``agent`` injected), and
        - bankroll_usdc > 0 (or ``force_capital_usdc``).

        On any failure, leaves deterministic path intact and returns False.
        """
        if agent is None and not self._llm_enabled:
            log.info("llm_wire_skipped", reason="no_deepseek_api_key")
            return False
        _cap = float(
            force_capital_usdc
            if force_capital_usdc is not None
            else (self.cfg.risk.bankroll_usdc or 0)
        )
        if _cap <= 0:
            log.info("llm_wire_skipped", reason="bankroll_unset")
            self._llm_enabled = False
            return False
        try:
            if agent is not None:
                self.deepseek_agent = agent  # type: ignore[assignment]
            else:
                self.deepseek_agent = DeepSeekAgent(api_key=self.cfg.secrets.deepseek_api_key)
            self.llm_gov = LLMGovernance(capital_usdc=_cap)
            self.gov_agent = GovernedDeepSeekAgent(self.deepseek_agent, self.llm_gov)  # type: ignore[arg-type]

            _mem_path = Path(self.cfg.paths.db).parent / "agent_memory.db"
            self.memory = AgentMemory(db_path=str(_mem_path))
            _recent = self.memory.get_recent(20)
            log.info("memory_loaded", n_recent=len(_recent))

            _hist_path = Path(self.cfg.paths.db).parent / "profile_history.db"
            self.profile_hist = ProfileHistory(str(_hist_path))

            self.self_improver = SelfImprover(
                history=self.profile_hist,
                memory=self.memory,  # type: ignore[arg-type]
                profile_name=next(iter(self.cfg.profiles), "default"),
            )
            if self.cfg.profiles:
                _first = self.cfg.profiles[next(iter(self.cfg.profiles))]
                if hasattr(self.self_improver, "set_live_profile"):
                    self.self_improver.set_live_profile(
                        _first.model_dump() if hasattr(_first, "model_dump") else {}
                    )

            # Facade: same chat_json_tool shape as DeepSeekAgent, but every
            # structured call is logged/sanitized through LLMGovernance.
            facade = _GovernedJsonFacade(self.deepseek_agent, self.llm_gov)
            self._gov_facade = facade

            self._discovery_agent = MarketDiscovery(
                agent=facade,  # type: ignore[arg-type]
                memory=self.memory,
                capital_usdc=_cap,
            )
            self.oversight_loop = OversightLoop(
                agent=facade,  # type: ignore[arg-type]
                memory=self.memory,
                snapshot_provider=self._oversight_snapshot,
            )
            self._llm_enabled = True
            model = getattr(self.deepseek_agent, "model", "mock")
            log.info(
                "llm_wired",
                capital_usdc=_cap,
                model=model,
                memory_n=len(_recent),
            )
            return True
        except Exception:  # noqa: BLE001
            log.exception("llm_wire_failed — running deterministic only")
            self._llm_enabled = False
            self.deepseek_agent = None
            self.gov_agent = None
            self.llm_gov = None
            self.oversight_loop = None
            self._discovery_agent = None
            return False

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

        # ── Minimum-capital gate: refuse to run if capital can't fund
        #     at least ONE market's reward-eligible order size. ──
        _cap = self._effective_capital
        if not self.paper and _cap > 0:
            _elapsed = False
            for _cid, meta in self.metas.items():
                r_min = getattr(meta, "rewards_min_size", 0.0) or 0.0
                p_typ = getattr(meta, "yes", None)
                p_typ = getattr(p_typ, "price", None) if p_typ else None
                p_typ = p_typ if p_typ and p_typ > 0 else 0.5
                min_notional = r_min * p_typ
                if min_notional > 0 and _cap >= min_notional:
                    _elapsed = True
                    break
            if not _elapsed and self.metas:
                _need = min(
                    (getattr(m, "rewards_min_size", 0.0) or 200) * 0.5
                    for m in self.metas.values()
                )
                log.critical(
                    "insufficient_capital",
                    capital_usdc=_cap,
                    min_required_per_market=round(_need, 2),
                    n_markets=len(self.metas),
                )
                self._running = False
                return
        # ──────────────────────────────────────────────────────────

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

        # ── V3 LLM wiring (governed DeepSeek when key + bankroll present) ──
        self.wire_llm_stack()
        # ──────────────────────────────────────────────────────────────

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

        # ── V3 LLM supervised loops (DeepSeek) ────────────────────────
        if self._llm_enabled and self.oversight_loop is not None:
            self._spawn("oversight", self._oversight_loop_task)
            self._spawn("improve", self._improve_loop)
            self._spawn("review", self._review_loop)
            if self._discovery_agent is not None:
                self._spawn("llm_discovery", self._llm_discovery_loop)
            log.info("llm_supervised_tasks_started")
        # ──────────────────────────────────────────────────────────────

        # Auto-balancer: shift capital toward high-reward markets every 10 min.
        # Runs even without LLM — it's pure math on reward accrual data.
        self._spawn("rebalancer", self._capital_rebalance_loop)

        self._spawn("supervisor", self._supervise)
        self.risk.reset_day()
        # ── Auto-compounding: track starting equity for growth scaling ──
        self._day_start_equity = self.risk.equity
        if self._day_start_equity > 0 and self._base_capital > 0:
            self._effective_capital = self._day_start_equity
            log.info("compounding_init",
                     base_capital=self._base_capital,
                     effective_capital=self._effective_capital)
        # ──────────────────────────────────────────────────────────────
        log.info(
            "engine_started",
            markets=len(self.metas),
            paper=self.paper,
            auto_discovery=self.cfg.engine.auto_discovery_enabled,
            hot_reload=self.cfg.engine.auto_discovery_hot_reload,
        )
        self._started_at = time.time()
        self._start_live_dashboard()

    def _start_live_dashboard(self) -> None:
        """Serve + open the localhost operator dashboard (paper and live)."""
        eng = self.cfg.engine
        if not getattr(eng, "dashboard_enabled", True):
            return
        try:
            from polymaker.metrics.live_dashboard import start_for_engine

            dash = start_for_engine(
                self,
                host=getattr(eng, "dashboard_host", "127.0.0.1"),
                port=int(getattr(eng, "dashboard_port", 8765)),
                open_browser=bool(getattr(eng, "dashboard_open_browser", True)),
            )
            # Persist the real URL (port may bump if busy) for operators/scripts.
            with contextlib.suppress(Exception):
                Path(self.cfg.paths.log_dir).mkdir(parents=True, exist_ok=True)
                (Path(self.cfg.paths.log_dir) / "dashboard.url").write_text(
                    dash.url + "\n", encoding="utf-8"
                )
            print(f"Dashboard: {dash.url}", flush=True)
            print(f"  healthz: curl -sf {dash.url}healthz", flush=True)
            log.info("dashboard_opened", url=dash.url, paper=self.paper)
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard_start_failed", err=str(exc))

    def _stop_live_dashboard(self) -> None:
        dash = getattr(self, "_live_dashboard", None)
        if dash is None:
            return
        with contextlib.suppress(Exception):
            dash.stop()
        self._live_dashboard = None
        with contextlib.suppress(Exception):
            url_path = Path(self.cfg.paths.log_dir) / "dashboard.url"
            if url_path.exists():
                url_path.unlink()

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
        self._stop_live_dashboard()
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
            markout=MultiHorizonMarkout(
                horizons_s=(30.0, 120.0, 300.0),
                weights=(0.5, 0.3, 0.2),
            ),
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
        self._llm_paused.discard(cid)
        self._pending_pause_cancels.discard(cid)
        self._last_regime.pop(cid, None)
        for tok in (meta.yes.token_id, meta.no.token_id):
            self._token_cid.pop(tok, None)
        # Tell the market data service to drop the subscription
        self.md.remove_market(cid)
        if self.user is not None:
            self.user.set_markets(list(self.metas))
        log.info("market_removed", condition_id=cid, slug=meta.slug)
        return True

    # ── V3 LLM oversight ─────────────────────────────────────────────

    def _oversight_snapshot(self) -> dict[str, Any]:
        """Build a rich snapshot for DeepSeek's 10-min oversight commentary.

        Includes everything DeepSeek needs to make informed decisions:
        equity/pnl trends, per-market reward share, regime state,
        fill quality, spread overrides, capital allocation, and
        adverse selection signals. No fluff — every field has a
        reason for being here.
        """
        equity = float(self.risk.equity)
        day_start = float(self.risk.day_start_equity or equity or 1.0)
        daily = float(self.risk.daily_pnl)
        drawdown = 0.0
        if day_start > 0 and equity < day_start:
            drawdown = (day_start - equity) / day_start

        # Per-market intelligence
        markets: dict[str, dict[str, Any]] = {}
        total_reward_accrued = 0.0
        for cid, meta in self.metas.items():
            yes_book = self.md.book(meta.yes.token_id)
            micro = yes_book.microprice(1) if yes_book is not None else None
            fv = float(micro) if micro is not None else 0.5
            spread = 0.0
            depth_imbalance = 0.0
            if yes_book is not None:
                v = yes_book.view()
                bb = v.best_bid.price if v.best_bid else 0
                ba = v.best_ask.price if v.best_ask else 0
                if bb > 0 and ba > 0:
                    spread = ba - bb
                bd = v.bid_depth if hasattr(v, "bid_depth") else 0
                ad = v.ask_depth if hasattr(v, "ask_depth") else 0
                total_depth = bd + ad
                depth_imbalance = (bd - ad) / max(total_depth, 0.01)

            # Regime + estimators
            regime_m = self.regime_m.get(cid)
            regime_state = str(getattr(regime_m, "current_regime", getattr(regime_m, "current", "QUIET")) if regime_m else "QUIET")

            est = self.est.get(cid)
            vol_ratio = float(getattr(est.vol, "ratio", 0) or 0)
            tox = float(getattr(est.markout, "toxicity", 0) or 0)
            flow_z = float(getattr(est.flow, "z", 0) or 0)

            # Capital + sizing
            alloc = float(self._discovery_capital.get(cid, 0) or 0)
            spread_override = float(self._per_market_spread_mult.get(cid, 1.0))
            profile = self.profiles.get(cid)
            base_size = float(getattr(profile, "base_size_usdc", 0) or 0) if profile else 0
            reward_min = float(getattr(meta, "rewards_min_size", 0) or 0)
            reward_rate = float(getattr(meta, "rewards_daily_rate", 0) or 0)

            # Fill quality
            fill_rate = 0.0
            resting_notional = 0.0
            orders = self.state.orders_for(meta.yes.token_id) + self.state.orders_for(meta.no.token_id)
            if orders:
                resting_notional = sum(o.price * o.size for o in orders)

            # Reward accrual (approximate from recent marks)
            reward_accrued = 0.0
            # Build a condensed market view
            markets[cid[:8]] = {
                "slug": str(getattr(meta, "slug", "") or ""),
                "fv": round(fv, 5),
                "spread_ticks": round(spread / max(meta.tick_size, 0.0001), 1),
                "regime": regime_state,
                "vol_ratio": round(vol_ratio, 4),
                "toxicity": round(tox, 4),
                "flow_z": round(flow_z, 3),
                "depth_imbalance": round(depth_imbalance, 3),
                "reward_min_size": reward_min,
                "reward_daily_rate": round(reward_rate, 2),
                "our_allocation_usdc": round(alloc, 2),
                "our_base_size_usdc": round(base_size, 2),
                "our_resting_notional_usdc": round(resting_notional, 2),
                "spread_override": round(spread_override, 2),
                "fill_rate": round(fill_rate, 4),
                "in_quarantine": cid in self._quarantined,
                "in_halted": cid in self._halted,
            }
            total_reward_accrued += reward_accrued

        return {
            "timestamp_utc": datetime.utcnow().isoformat(),
            "paper_mode": self.paper,
            # Portfolio level
            "equity": round(equity, 2),
            "daily_pnl": round(daily, 2),
            "daily_return_pct": round(daily / max(day_start, 1) * 100, 2),
            "drawdown_pct": round(drawdown * 100, 2),
            "effective_capital": round(self._effective_capital, 2),
            "base_capital": round(self._base_capital, 2),
            "capital_compounded": self._effective_capital > self._base_capital * 1.01,
            # Risk envelope
            "daily_loss_kill_usdc": round(self.cfg.risk.daily_loss_kill_usdc, 2),
            "days_until_loss_kill": (
                round(abs(self.cfg.risk.daily_loss_kill_usdc / max(daily, 0.01)), 1)
                if daily < 0 else None
            ),
            # Activity
            "n_active_markets": len(self.metas),
            "n_halted": len(self._halted),
            "n_quarantined": len(self._quarantined),
            "llm_enabled": bool(self._llm_enabled),
            # Per-market data (richest field)
            "markets": markets,
        }

    def _recent_fill_rate(self) -> float:
        """Simple rolling fill-rate estimate from resting + fill history."""
        orders = list(getattr(self.state, "orders", {}).values())
        if not orders:
            return 0.0
        filled = sum(
            1 for o in orders
            if float(getattr(o, "filled_size", 0) or 0) > 0
        )
        return filled / max(1, len(orders))

    @staticmethod
    def pack_oversight_action(action: Any) -> dict[str, Any]:
        """Pack an OversightAction (or dict) into the apply_oversight_action payload.

        Production path: used by :meth:`run_oversight_cycle_once`. Must pass
        ``params`` / ``spread_mult`` so widen/tighten are not no-ops.
        """
        if isinstance(action, dict):
            atype = str(action.get("type") or action.get("action") or "no_op")
            cid = str(
                action.get("condition_id")
                or action.get("cid")
                or action.get("market")
                or ""
            )
            reason = str(action.get("reason") or "")
            params = dict(action.get("params") or {})
            spread_mult = action.get("spread_mult")
            if spread_mult is None:
                spread_mult = params.get("mult", params.get("spread_mult"))
            out: dict[str, Any] = {
                "type": atype,
                "condition_id": cid,
                "reason": reason,
                "params": params,
            }
            if spread_mult is not None:
                out["spread_mult"] = float(spread_mult)
            for k in ("side", "direction", "buy_this_market"):
                if k in action:
                    out[k] = action[k]
            return out

        # OversightAction dataclass
        params = dict(getattr(action, "params", None) or {})
        mult = params.get("mult", params.get("spread_mult"))
        payload: dict[str, Any] = {
            "type": str(getattr(action, "type", "no_op")),
            "condition_id": str(getattr(action, "market", None) or ""),
            "reason": str(getattr(action, "reason", "") or ""),
            "params": params,
        }
        if mult is not None:
            payload["spread_mult"] = float(mult)
        return payload

    def apply_oversight_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """Apply a single oversight action from the OversightLoop.

        Called by :meth:`run_oversight_cycle_once` after
        :meth:`pack_oversight_action`. Action types:
        - tighten_spread / widen_spread : adjust spread_mult on profile
        - pause_market : ops/LLM pause (``_llm_paused``; survives Gamma refresh)
        - resume_market : clear ops pause (optional)
        - add_layer / drop_market : deferred (self-improve / paper)
        - no_op : acknowledged

        Directional fields are hard-rejected. Knob nudges go through
        :class:`LLMGovernance` when wired.
        """
        atype = str(action.get("type") or action.get("action") or "no_op")
        cid = str(
            action.get("condition_id")
            or action.get("cid")
            or action.get("market")
            or ""
        )
        reason = str(action.get("reason") or "")
        params = dict(action.get("params") or {})

        # Hard reject directional steer at apply time (AC2).
        for bad in ("side", "direction", "buy_this_market", "buy_yes", "buy_no"):
            if bad in action or bad in params:
                log.warning("oversight_directional_rejected", type=atype, field=bad)
                return {
                    "action": atype,
                    "status": "rejected_directional",
                    "reason": f"directional_field:{bad}",
                }

        if atype == "no_op":
            return {"action": "no_op", "status": "acknowledged"}

        # Ops/LLM pause: durable set, not Gamma ``_halted`` (metadata refresh
        # discards accepting markets from ``_halted`` and would wipe a pause).
        if atype == "pause_market":
            if not cid:
                return {"action": "pause_market", "status": "unknown", "reason": "missing_cid"}
            self._llm_paused.add(cid)
            self._pending_pause_cancels.add(cid)
            self._wake_cid(cid)  # force recompute ASAP → empty targets while paused
            log.info("oversight_pause_market", cid=cid[:8], reason=reason)
            return {
                "action": "pause_market",
                "status": "applied",
                "cid": cid[:8],
                "needs_cancel": True,
            }

        if atype == "resume_market":
            if not cid:
                return {"action": "resume_market", "status": "unknown", "reason": "missing_cid"}
            self._llm_paused.discard(cid)
            self._pending_pause_cancels.discard(cid)
            self._wake_cid(cid)
            log.info("oversight_resume_market", cid=cid[:8], reason=reason)
            return {"action": "resume_market", "status": "applied", "cid": cid[:8]}

        if atype in ("tighten_spread", "widen_spread"):
            if cid and cid not in self.profiles:
                return {
                    "action": atype,
                    "status": "unknown",
                    "reason": f"unknown_market:{cid[:8]}",
                }
            # Prefer explicit spread_mult, then params.mult / params.spread_mult
            raw_mult = action.get("spread_mult")
            if raw_mult is None:
                raw_mult = params.get("mult", params.get("spread_mult", 1.0))
            try:
                mult = float(raw_mult)
            except (TypeError, ValueError):
                mult = 1.0
            # tighten → compress toward 0.5; widen → expand toward 3.0 when
            # the LLM only sent a relative mult without absolute value.
            if atype == "tighten_spread" and mult >= 1.0 and "spread_mult" not in action:
                mult = max(0.5, 1.0 / mult) if mult > 0 else 0.75
            mult = max(0.5, min(3.0, mult))

            # Governance gate for knob (logs + clamps when LLM gov is live).
            if self.llm_gov is not None:
                import time as _time

                decision = self.llm_gov.check_and_log(
                    prompt=f"oversight_apply:{atype}:{cid[:8]}",
                    response={"actions": {"spread_mult": mult}},
                    llm_started_at=_time.time(),
                    context={"kind": "oversight_apply", "condition_id": cid, "type": atype},
                    confidence=1.0,
                )
                if not decision.approved:
                    return {
                        "action": atype,
                        "status": "rejected_by_governance",
                        "reason": decision.rejection_reason,
                    }
                if "spread_mult" in decision.actions:
                    with contextlib.suppress(TypeError, ValueError):
                        mult = float(decision.actions["spread_mult"])
                    mult = max(0.5, min(3.0, mult))

            if not cid:
                return {"action": atype, "status": "unknown", "reason": "missing_cid"}
            self._per_market_spread_mult[cid] = mult
            log.info(
                "oversight_spread_adjust",
                cid=cid[:8],
                mult=round(mult, 2),
                reason=reason,
            )
            return {
                "action": atype,
                "status": "applied",
                "cid": cid[:8],
                "spread_mult": mult,
            }

        if atype in ("add_layer", "drop_market"):
            log.info("oversight_deferred", action=atype, cid=cid[:8], reason=reason)
            return {"action": atype, "status": "deferred_to_self_improve", "cid": cid[:8]}

        # ── DeepSeek trading authority: sizing, aggression, band, rotation ──

        if atype == "size_up" or atype == "size_down":
            if not cid:
                return {"action": atype, "status": "unknown", "reason": "missing_cid"}
            mult = max(0.5, min(2.0, float(params.get("mult", 1.0) or 1.0)))
            cur = float(self._grok_aggression.get(cid, 1.0))
            if atype == "size_up":
                cur = min(2.0, cur * mult)
            else:
                cur = max(0.5, cur / max(mult, 1.01))
            self._grok_aggression[cid] = cur
            self._wake_cid(cid)
            log.info("llm_size_adjust", cid=cid[:8], aggression=round(cur, 2), mult=mult, reason=reason)
            return {"action": atype, "status": "applied", "cid": cid[:8], "aggression": cur}

        if atype == "go_aggressive" or atype == "go_defensive":
            if not cid:
                return {"action": atype, "status": "unknown", "reason": "missing_cid"}
            band = params.get("band", params.get("band_position"))
            if atype == "go_aggressive":
                band = float(band) if band is not None else 0.65
                band = max(0.5, min(0.8, band))
            else:
                band = float(band) if band is not None else 0.25
                band = max(0.1, min(0.4, band))
            cur_aggression = self._grok_aggression.get(cid, 1.0)
            if atype == "go_aggressive":
                self._grok_aggression[cid] = min(2.0, cur_aggression * 1.3)
            else:
                self._grok_aggression[cid] = max(0.5, cur_aggression * 0.7)
            self._grok_band_override[cid] = band
            self._wake_cid(cid)
            log.info("llm_stance_adjust", cid=cid[:8], band=round(band, 2),
                     aggression=round(self._grok_aggression[cid], 2), reason=reason)
            return {"action": atype, "status": "applied", "cid": cid[:8],
                    "band_position": band, "aggression": self._grok_aggression[cid]}

        if atype == "rotate_capital":
            src = str(params.get("from") or params.get("src") or "")
            dst = str(params.get("to") or params.get("dst") or "")
            amt = float(params.get("amount", params.get("amt", 0)) or 0)
            if src and dst and amt > 0:
                src_cap = float(self._discovery_capital.get(src, 0) or 0)
                if src_cap >= amt:
                    self._discovery_capital[src] = src_cap - amt
                    self._discovery_capital[dst] = float(self._discovery_capital.get(dst, 0)) + amt
                    log.info("llm_rotate_capital", src=src[:8], dst=dst[:8],
                             amt=round(amt, 2), reason=reason)
                    return {"action": "rotate_capital", "status": "applied",
                            "src": src[:8], "dst": dst[:8], "amount": amt}
            return {"action": "rotate_capital", "status": "rejected", "reason": "invalid_params"}

        if atype == "set_trigger":
            from polymaker.intelligence.deepseek_triggers import (
                TRIGGER_ACTIONS,
                TRIGGER_CONDITIONS,
                DeepSeekTrigger,
            )
            cond = str(params.get("condition") or params.get("cond") or "")
            thresh = float(params.get("threshold", params.get("thresh", 0)) or 0)
            trig_action = str(params.get("trigger_action") or params.get("action") or "alert_only")
            market = str(params.get("market") or cid or "")
            if cond not in TRIGGER_CONDITIONS or trig_action not in TRIGGER_ACTIONS:
                return {"action": "set_trigger", "status": "rejected",
                        "reason": f"invalid_condition({cond})_or_action({trig_action})"}
            trig = DeepSeekTrigger(
                condition=cond, threshold=thresh, action=trig_action,
                market=market, reason=reason, set_by="grok",
            )
            self._deepseek_triggers.append(trig)
            log.info("llm_trigger_set", condition=cond, threshold=thresh,
                     action=trig_action, market=market[:8] if market else "portfolio")
            return {"action": "set_trigger", "status": "applied",
                    "condition": cond, "threshold": thresh, "trigger_action": trig_action}

        return {"action": atype, "status": "unknown", "reason": reason}

    # ── V3 supervised loops ─────────────────────────────────────────

    def is_quoting_halted(self, cid: str) -> bool:
        """True if market must not place new maker quotes.

        Combines Gamma closed/not-accepting (``_halted``) with durable ops/LLM
        pause (``_llm_paused``). Metadata refresh may clear ``_halted`` for
        accepting markets but never clears ``_llm_paused``.
        """
        return cid in self._halted or cid in self._llm_paused

    async def flush_pause_cancels(self) -> int:
        """Cancel resting orders for markets pending ops/LLM pause.

        Called from the oversight cycle after :meth:`apply_oversight_action`
        marks ``needs_cancel``. Safe to call repeatedly.
        """
        n = 0
        for cid in list(self._pending_pause_cancels):
            self._pending_pause_cancels.discard(cid)
            meta = self.metas.get(cid)
            if meta is None:
                continue
            await self._cancel_market_orders(cid, meta, reason="llm_pause")
            n += 1
        return n

    async def run_oversight_cycle_once(self) -> list[dict[str, Any]]:
        """One production oversight cycle: snapshot → LLM → pack → apply.

        Extracted so unit tests drive the **same** packing/apply path as
        :meth:`_oversight_loop_task` (no special-cased test payload).
        Pause actions cancel resting orders before return.
        """
        if self.oversight_loop is None:
            return []
        snapshot = self._oversight_snapshot()
        await self.oversight_loop.run_once(snapshot)
        actions = self.oversight_loop.drain_actions(include_dry_run=False)
        results: list[dict[str, Any]] = []
        for a in actions:
            payload = self.pack_oversight_action(a)
            result = self.apply_oversight_action(payload)
            log.info("oversight_action_applied", result=result)
            results.append(result)
        # Immediate cancel for pause_market (do not wait for incidental recompute)
        if self._pending_pause_cancels:
            await self.flush_pause_cancels()
        return results

    async def _oversight_loop_task(self) -> None:
        """Continuous DeepSeek oversight — runs 24/7 with 30s between calls.

        DeepSeek is cheap enough ($0.96/day at 2K tokens/call, 60 calls/hr)
        for continuous strategic awareness. The trigger system provides
        sub-second automated reactions (zero API cost). DeepSeek provides
        the continuous strategic layer on top.
        """
        if self.oversight_loop is None:
            return
        while self._running:
            try:
                await self.run_oversight_cycle_once()
            except Exception:
                log.exception("oversight_loop_error")
            await asyncio.sleep(30)  # continuous — DeepSeek sees every market move

    async def _improve_loop(self) -> None:
        """Auto self-improve: every 6h or on strategy decay."""
        if self.self_improver is None:
            return
        while self._running:
            try:
                should_run = False
                reason = "time"
                now = time.time()
                if now - self._last_improve_ts > 21600:
                    should_run = True
                if should_run and self._last_improve_ts < now - 300:
                    eval_ = self._build_self_evaluation()
                    try:
                        result = self.self_improver.run(eval_, force=True)
                        if result.triggered and result.suggestion:
                            log.info("improve_suggestion",
                                     diagnosis=result.suggestion.diagnosis,
                                     suggestion=result.suggestion.suggestion)
                            if self.llm_gov is not None:
                                _ = self.llm_gov.critique_prompt(
                                    suggestion=result.suggestion.suggestion,
                                    actions=result.suggestion.profile_overrides,
                                    context={"reason": reason},
                                )
                            if self.memory is not None:
                                self.memory.add(
                                    content=f"self_improve[{reason}]: {result.suggestion.diagnosis}",
                                    kind="insight", confidence=0.7,
                                )
                    except Exception:
                        log.exception("improve_loop_error")
                    self._last_improve_ts = now
                await asyncio.sleep(600)
            except Exception:
                log.exception("improve_loop_error")

    async def _review_loop(self) -> None:
        """Auto daily review at ~23:55 UTC."""
        from polymaker.intelligence.review import (
            gather_day_summary,
            render_markdown,
            run_daily_review,
            should_run_eod_review,
        )
        if self.memory is None:
            return
        while self._running:
            try:
                if should_run_eod_review():
                    summary = gather_day_summary(
                        db_path=self.cfg.paths.db,
                        memory=self.memory,
                    )
                    result = run_daily_review(
                        summary=summary,
                        memory=self.memory,
                        api_key=self.cfg.secrets.deepseek_api_key,
                    )
                    _md = render_markdown(summary, result)
                    log.info("daily_review_complete", grade=result.grade)
                    self._last_review_ts = time.time()
                await asyncio.sleep(300)
            except Exception:
                log.exception("review_loop_error")

    async def run_llm_discovery_cycle_once(self) -> list[Any]:
        """One discovery cycle: rank candidates → selection input for trade list.

        Returns applied rankings (may be empty). Unit-testable production path.
        """
        if self._discovery_agent is None:
            return []
        candidates = self.catalog.top(limit=50)
        if not candidates:
            return []
        metas: list[Any] = []
        meta_by_cid: dict[str, Any] = {}
        for row in candidates:
            meta = row[0] if isinstance(row, tuple) else row
            cid = getattr(meta, "condition_id", "") or ""
            if cid:
                meta_by_cid[cid] = meta
            metas.append({
                "slug": getattr(meta, "slug", "") or "",
                "condition_id": cid,
                "question": getattr(meta, "question", "") or "",
                "rewards_min_size": getattr(meta, "rewards_min_size", 0),
                "rewards_daily_rate": getattr(meta, "rewards_daily_rate", 0),
                "liquidity_num": getattr(meta, "liquidity_num", 0),
                "min_order_size": getattr(meta, "min_order_size", 5.0),
                "best_bid": getattr(meta, "best_bid", 0.0),
                "best_ask": getattr(meta, "best_ask", 0.0),
            })
        result = await self._discovery_agent.rank_candidates(metas)
        rankings = list(getattr(result, "rankings", []) or [])
        self._llm_rankings = rankings
        if rankings:
            log.info(
                "llm_discovery_ranked",
                n_total=len(candidates),
                n_top=len(rankings),
            )
            await self._apply_llm_rankings(rankings, meta_by_cid)
        return rankings

    async def _apply_llm_rankings(
        self,
        rankings: list[Any],
        meta_by_cid: dict[str, Any],
    ) -> int:
        """Feed MarketDiscovery rankings into trade-list add / capital preference.

        AC1: LLM ranking is selection input, not log-only.
        - Reward capital gate must pass.
        - ``suggested_size_pct`` capped via governance when available.
        - Adds missing markets up to auto_discovery_max_markets.
        """
        bankroll = float(
            self.cfg.risk.bankroll_usdc
            or self._effective_capital
            or 0.0
        )
        if bankroll <= 0:
            log.info("llm_selection_skip", reason="bankroll_unset")
            return 0
        profile_name = self.cfg.engine.auto_discovery_profile
        profile = self.cfg.profiles.get(profile_name)
        if profile is None and self.cfg.profiles:
            profile = next(iter(self.cfg.profiles.values()))
        if profile is None:
            profile = StrategyProfile()
        # No hard market cap — DeepSeek decides count. Capital gate is the only limit.
        added = 0
        for rank in rankings:
            cid = str(getattr(rank, "condition_id", "") or "")
            if not cid:
                continue
            meta = meta_by_cid.get(cid) or self.catalog.get(cid)
            if meta is None:
                continue
            conf = float(getattr(rank, "confidence", 0.0) or 0.0)
            size_pct = float(getattr(rank, "suggested_size_pct", 0.0) or 0.0)
            size_pct = max(0.0, min(1.0, size_pct))

            # Governance: size_pct + market_selection eligibility
            if self.llm_gov is not None:
                typ_px = 0.5
                bb = float(getattr(meta, "best_bid", 0) or 0)
                ba = float(getattr(meta, "best_ask", 0) or 0)
                if bb > 0 and ba > 0:
                    typ_px = 0.5 * (bb + ba)
                per_cap = float(self.cfg.risk.max_market_notional_usdc or bankroll * 0.35)
                decision = self.llm_gov.check_and_log(
                    prompt=f"llm_select:{cid[:8]}",
                    response={"actions": {"size_pct": size_pct}},
                    llm_started_at=time.time(),
                    context={
                        "kind": "market_selection",
                        "condition_id": cid,
                        "rewards_min_size": float(getattr(meta, "rewards_min_size", 0) or 0),
                        "typical_price": typ_px,
                        "per_market_cap_usdc": per_cap,
                    },
                    confidence=conf,
                )
                if not decision.approved:
                    log.info(
                        "llm_selection_rejected",
                        condition_id=cid,
                        reason=decision.rejection_reason,
                    )
                    continue
                if "size_pct" in decision.actions:
                    with contextlib.suppress(TypeError, ValueError):
                        size_pct = float(decision.actions["size_pct"])
                elif decision.size_pct_after_cap > 0:
                    size_pct = decision.size_pct_after_cap

            gate = decide_maker_reward_eligibility(
                bankroll_usdc=bankroll,
                rewards_min_size=float(getattr(meta, "rewards_min_size", 0) or 0),
                exchange_min_shares=float(getattr(meta, "min_order_size", 5) or 5),
                typical_price=(
                    0.5 * (float(getattr(meta, "best_bid", 0) or 0)
                           + float(getattr(meta, "best_ask", 0) or 0))
                    if float(getattr(meta, "best_bid", 0) or 0) > 0
                    else 0.5
                ),
                layers=int(getattr(profile, "layers", 1) or 1),
                reward_size_mult=float(getattr(profile, "reward_size_mult", 1.0) or 1.0),
            )
            self._reward_eligibility[cid] = gate
            if gate.skip:
                log.info(
                    "llm_selection_skip_capital",
                    condition_id=cid,
                    reason=gate.reason,
                )
                continue

            # DeepSeek's suggested_size_pct is the authority. No floor — DeepSeek
            # decides the allocation. Capital gate ensures reward eligibility.
            alloc = max(bankroll * max(size_pct, 0.001), gate.recommended_base_size_usdc)
            self._discovery_capital[cid] = min(alloc, bankroll * 0.4)

            if cid in self.metas:
                # Already trading: DeepSeek updates size preference on profile.
                # DeepSeek's allocation is authoritative; don't max(old, gate).
                cur = self.profiles.get(cid, profile)
                grok_size = bankroll * size_pct
                self.profiles[cid] = cur.model_copy(update={
                    "base_size_usdc": max(
                        grok_size,
                        gate.recommended_base_size_usdc or cur.base_size_usdc,
                    ),
                    "bankroll_usdc": self._discovery_capital[cid],
                })
                continue

            grok_base = bankroll * size_pct
            mkt_profile = profile.model_copy(update={
                "q_max_usdc": min(
                    profile.q_max_usdc,
                    self._discovery_capital[cid],
                ),
                "base_size_usdc": max(
                    profile.base_size_usdc,
                    grok_base,
                    gate.recommended_base_size_usdc or profile.base_size_usdc,
                ),
                "bankroll_usdc": self._discovery_capital[cid],
            })
            ok = await self.add_market(meta, mkt_profile)
            if ok:
                added += 1
                log.info(
                    "llm_selection_added",
                    condition_id=cid,
                    slug=getattr(meta, "slug", ""),
                    size_pct=round(size_pct, 3),
                    allocated_usdc=round(self._discovery_capital[cid], 2),
                )
        return added

    async def _llm_discovery_loop(self) -> None:
        """Continuous DeepSeek market discovery — new markets every 90s."""
        if self._discovery_agent is None:
            return
        while self._running:
            try:
                await self.run_llm_discovery_cycle_once()
                await asyncio.sleep(90)  # 90s — DeepSeek finds markets near-instantly
            except Exception:
                log.exception("llm_discovery_loop_error")
                await asyncio.sleep(30)

    async def _capital_rebalance_loop(self) -> None:
        """Shift capital toward markets with highest reward accrual.

        Every 10 min, compute reward-per-dollar for each active market
        and redistribute unallocated capital + trim the lowest performers.
        Runs even without LLM — it's pure arithmetic on the metrics log.
        """
        import json

        while self._running:
            await asyncio.sleep(600)  # 10 min
            try:
                if len(self.metas) <= 1:
                    continue

                # Read the last ~100 reward accrual events from metrics
                reward_map: dict[str, float] = {}
                metrics_path = Path(self.cfg.paths.log_dir) / (
                    "metrics-paper.jsonl" if self.paper else "metrics-live.jsonl"
                )
                if not metrics_path.exists():
                    continue
                # Parse last N lines efficiently (tail-read)
                lines: list[str] = []
                with metrics_path.open() as fh:
                    fh.seek(0, 2)  # seek to end
                    pos = fh.tell()
                    while pos > 0 and len(lines) < 200:
                        pos = max(0, pos - 8192)
                        fh.seek(pos)
                        chunk = fh.read(min(8192, fh.tell() - pos if fh.tell() > 0 else 8192))
                        lines = chunk.splitlines() + lines
                        if pos == 0:
                            break
                        fh.seek(pos)

                for raw in lines[-200:]:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("event") != "mark":
                        continue
                    cid = obj.get("condition_id", "")
                    reward = float(obj.get("reward_accrued_usdc", 0.0) or 0.0)
                    if reward > 0 and cid:
                        reward_map[cid] = reward_map.get(cid, 0.0) + reward

                if not reward_map:
                    continue

                # Find top 3 and bottom 3 by reward
                ranked = sorted(reward_map.items(), key=lambda x: x[1], reverse=True)
                top = {cid for cid, _ in ranked[:3]}
                bottom = {cid for cid, _ in ranked
                          if cid not in top and ranked.index((cid, _)) >= len(ranked) // 2}

                # Reallocate: shift 10% of capital from bottom performers to top
                if bottom and top:
                    shift_pct = 0.10
                    for cid in bottom:
                        if cid in self._discovery_capital:
                            old = self._discovery_capital[cid]
                            cut = old * shift_pct
                            self._discovery_capital[cid] = max(old - cut, old * 0.5)
                            surplus = cut
                            # Give to the top market
                            top_cid = next(iter(top))
                            self._discovery_capital[top_cid] = (
                                self._discovery_capital.get(top_cid, 0) + surplus
                            )
                    log.info("rebalance",
                             top=round(sum(v for k, v in ranked[:3]), 2),
                             bottom=round(sum(v for k, v in ranked[-3:]), 2),
                             shifted_pct=shift_pct)
            except Exception:
                log.exception("rebalance_loop_error")

    def _apply_trigger_action(self, violation: Any) -> None:
        """Execute a triggered DeepSeek guardrail — zero API calls."""
        cid = getattr(violation.trigger, "market", None) or ""
        action = getattr(violation.trigger, "action", "alert_only")
        mult = float(getattr(violation.trigger, "mult", 0.7) or 0.7)

        if action == "pause" and cid:
            self._llm_paused.add(cid)
            self._pending_pause_cancels.add(cid)
            self._wake_cid(cid)
            log.info("trigger_pause", cid=cid[:8],
                     condition=violation.trigger.condition)

        elif action == "defensive" and cid:
            self._grok_aggression[cid] = max(0.5, float(self._grok_aggression.get(cid, 1.0) or 1.0) * 0.7)
            self._grok_band_override[cid] = 0.25
            self._wake_cid(cid)
            log.info("trigger_defensive", cid=cid[:8],
                     aggression=round(self._grok_aggression[cid], 2))

        elif action == "size_down" and cid:
            cur = float(self._grok_aggression.get(cid, 1.0) or 1.0)
            self._grok_aggression[cid] = max(0.5, cur * mult)
            self._wake_cid(cid)
            log.info("trigger_size_down", cid=cid[:8], mult=mult)

        elif action == "size_up" and cid:
            cur = float(self._grok_aggression.get(cid, 1.0) or 1.0)
            self._grok_aggression[cid] = min(2.0, cur / max(mult, 0.01))
            self._wake_cid(cid)
            log.info("trigger_size_up", cid=cid[:8], mult=mult)

        # alert_only: logged by evaluate_triggers, no engine action

    def _build_self_evaluation(self) -> Any:
        """Build a minimal SelfEvaluation from engine state for self-improve."""
        from polymaker.intelligence.self_eval import SelfEvaluation

        eval_ = SelfEvaluation()
        eval_.update(pnl=self.risk.daily_pnl, regime="QUIET", offset="BUY_2")
        return eval_

    # ── Discovery + hot-reload ─────────────────────────────────────────

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
        max_markets = min(50, int(self.cfg.engine.auto_discovery_max_markets or 50))
        tags = tuple(self.cfg.engine.auto_discovery_tags) or ("politics",)
        profile_name = self.cfg.engine.auto_discovery_profile
        min_liquidity = float(getattr(self.cfg.engine, "auto_discovery_min_liquidity", 10000.0))
        min_daily_rate = float(getattr(self.cfg.engine, "auto_discovery_min_daily_rate", 10.0))
        max_spread = float(getattr(self.cfg.engine, "auto_discovery_max_spread_cents", 5.0))

        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._run_discovery_pass(
                    tags, min_score, max_markets, profile_name,
                    min_liquidity, min_daily_rate, max_spread,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("discovery_pass_failed", err=str(exc))

    async def _run_discovery_pass(
        self, tags: tuple[str, ...], min_score: float, max_markets: int, profile_name: str,
        min_liquidity: float = 10000.0, min_daily_rate: float = 10.0,
        max_spread_cents: float = 5.0,
    ) -> None:
        """One pass of market discovery: scan, score, add new markets, remove closed.

        Profitability gates (added per user audit):
        - min_liquidity: skip markets with less than this liquidity (USDC)
        - min_daily_rate: skip markets with less than this daily reward rate
        - max_spread_cents: skip markets with reward band wider than this
          (wide band = high adverse-selection risk)
        """
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

        # Score and filter with profitability gates
        scored: list[tuple[float, MarketMeta]] = []
        for meta in scanned:
            sc = score_market(meta, bankroll_usdc=float(self.cfg.risk.bankroll_usdc or 100.0))
            if sc.score < min_score:
                continue
            if meta.liquidity_num < min_liquidity:
                log.debug("skip_low_liquidity", slug=meta.slug, liq=meta.liquidity_num)
                continue
            if meta.rewards_daily_rate < min_daily_rate:
                log.debug("skip_low_reward", slug=meta.slug, rate=meta.rewards_daily_rate)
                continue
            if meta.rewards_max_spread > max_spread_cents:
                log.debug("skip_wide_spread", slug=meta.slug, spread=meta.rewards_max_spread)
                continue
            scored.append((sc.score, meta))

        # Multi-market dominator: max Σ risk-adjusted share-adj under bankroll.
        # Dynamic slots (best N for capital) capped by discovery max_markets.
        from polymaker.catalog.scoring import adverse_selection_risk
        from polymaker.strategy.share_planning import (
            optimize_multi_market_portfolio,
            recommend_max_markets,
        )

        bankroll = float(self.cfg.risk.bankroll_usdc or self.cfg.risk.max_total_exposure_usdc or 100.0)
        by_cid = {m.condition_id: m for _, m in scored}
        cand_dicts: list[dict[str, Any]] = []
        for sc_val, meta in scored:
            mid = 0.5
            if meta.best_bid > 0 and meta.best_ask > 0:
                mid = 0.5 * (meta.best_bid + meta.best_ask)
            cand_dicts.append({
                "condition_id": meta.condition_id,
                "rewards_daily_rate": float(meta.rewards_daily_rate or 0.0),
                "rewards_min_size": float(meta.rewards_min_size or 0.0),
                "liquidity_num": float(meta.liquidity_num or 0.0),
                "typical_price": mid,
                "min_order_size": float(meta.min_order_size or 5.0),
                "rewards_max_spread": float(meta.rewards_max_spread or 0.0),
                "as_risk": float(adverse_selection_risk(meta)),
                "end_date_iso": getattr(meta, "end_date_iso", None),
                "score": sc_val,
            })
        conc = float(self.cfg.risk.max_market_concentration_pct or 0.4)
        deploy = float(getattr(self.cfg.risk, "capital_deploy_frac", 0.6) or 0.6)
        horizon = float(getattr(self.cfg.risk, "prefer_horizon_days", 14.0) or 0.0)
        dyn_n = recommend_max_markets(
            cand_dicts,
            bankroll_usdc=bankroll,
            hard_cap=int(max_markets),
            max_concentration=conc,
            capital_deploy_frac=deploy,
            prefer_horizon_days=horizon,
        )
        port = optimize_multi_market_portfolio(
            cand_dicts,
            bankroll_usdc=bankroll,
            max_markets=dyn_n,
            max_concentration=conc,
            auto_max_markets=False,
            hard_cap_markets=int(max_markets),
            capital_deploy_frac=deploy,
            prefer_horizon_days=horizon,
        )
        if port.picks:
            ordered = [
                by_cid[p.condition_id]
                for p in port.picks
                if p.condition_id in by_cid
            ]
            # Append any scored markets not picked (fallback order by score)
            picked = {p.condition_id for p in port.picks}
            for _sc_val, meta in sorted(scored, key=lambda x: -x[0]):
                if meta.condition_id not in picked:
                    ordered.append(meta)
            self._discovery_capital = {
                p.condition_id: p.allocated_usdc for p in port.picks
            }
            log.info(
                "discovery_portfolio",
                n_picks=port.n_markets,
                total_share_adj=round(port.total_share_adjusted_usdc, 4),
                daily_return_pct=round(port.daily_return_pct, 6),
                bankroll=bankroll,
            )
        else:
            ordered = [m for _, m in sorted(scored, key=lambda x: -x[0])]
            self._discovery_capital = {}

        auto_count = sum(
            1 for cid in self.metas
            if cid not in {e.condition_id for e in self.cfg.enabled_markets if e.condition_id}
        )
        added = 0
        for meta in ordered:
            if meta.condition_id in self.metas:
                continue
            if auto_count + added >= max_markets:
                break
            await self._apply_meta_to_market(meta)
            # Scale profile q_max/base to allocated capital when available
            mkt_profile = profile
            cap = float(self._discovery_capital.get(meta.condition_id, 0.0) or 0.0)
            if cap > 0:
                mkt_profile = profile.model_copy(update={
                    "q_max_usdc": min(profile.q_max_usdc, cap),
                    "base_size_usdc": min(profile.base_size_usdc, max(2.0, cap * 0.25)),
                    "bankroll_usdc": cap,
                })
            # Refuse markets we cannot fund at rewardsMinSize (two-sided).
            gate_bankroll = cap if cap > 0 else bankroll
            typ_px = 0.5
            if meta.best_bid > 0 and meta.best_ask > 0:
                typ_px = 0.5 * (meta.best_bid + meta.best_ask)
            gate = decide_maker_reward_eligibility(
                bankroll_usdc=gate_bankroll,
                rewards_min_size=float(meta.rewards_min_size or 0.0),
                exchange_min_shares=float(meta.min_order_size or 5.0),
                typical_price=typ_px,
                layers=int(mkt_profile.layers or 1),
                reward_size_mult=float(mkt_profile.reward_size_mult or 1.0),
                default_base_size_usdc=float(mkt_profile.base_size_usdc or 0.0),
            )
            self._reward_eligibility[meta.condition_id] = gate
            if gate.skip:
                log.info(
                    "auto_market_skip_capital",
                    condition_id=meta.condition_id,
                    slug=meta.slug,
                    reason=gate.reason,
                    bankroll_usdc=gate.bankroll_usdc,
                    rewards_min_size=meta.rewards_min_size,
                )
                continue
            if gate.eligible and gate.recommended_base_size_usdc > 0:
                mkt_profile = mkt_profile.model_copy(update={
                    "base_size_usdc": max(
                        mkt_profile.base_size_usdc, gate.recommended_base_size_usdc
                    ),
                })
            ok = await self.add_market(meta, mkt_profile)
            if ok:
                added += 1
                log.info(
                    "auto_market_added",
                    condition_id=meta.condition_id,
                    slug=meta.slug,
                    score=round(score_market(meta).score, 4),
                    allocated_usdc=round(cap, 2),
                    reward_eligible=gate.eligible,
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
        meta = self.metas[cid]
        p = self.profiles[cid]
        self.est[cid].flow.update(tp.aggressor, tp.size, tp.ts)
        # Toxicity / impact estimators (fed here; quote consumption is Tier-2)
        book = self.md.book(tp.asset_id)
        mid = 0.0
        if book is not None:
            bb0, ba0 = book.best_bid(), book.best_ask()
            if bb0 is not None and ba0 is not None:
                mid = 0.5 * (bb0.price + ba0.price)
        self.est[cid].on_trade_print(tp.aggressor, tp.size, mid, float(tp.ts or time.time()))
        # Feed judgment layer: trade history for dead-tape / microstructure
        ts = float(tp.ts or time.time())
        hist = self._trade_ts.setdefault(cid, [])
        hist.append(ts)
        # Keep ~1h of trade times
        cutoff = ts - 3600.0
        self._trade_ts[cid] = [t for t in hist if t >= cutoff]
        if p.use_intelligence:
            side = "BUY" if tp.aggressor is Side.BUY else "SELL"
            self.intel.update_trade(cid, side, tp.price, tp.size, ts)

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
        base = p.base_size_usdc / max(tp.price, meta.tick_size)
        if tp.size < p.event_sweep_mult * base:
            return
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
        """Match a trade print against paper-mode resting orders and process fills.

        Caller is responsible for acquiring self._fill_sim_lock to avoid
        concurrent mutation of the fill simulator's internal state.
        """
        fills = self._fill_sim.match(tp.asset_id, tp.aggressor, tp.price, tp.size, tp.ts)
        if not fills:
            return
        cid = self._token_cid.get(tp.asset_id)
        if cid is None:
            return
        for fill in fills:
            if not self.state.apply_fill(fill):
                continue  # duplicate (shouldn't happen in paper, but be safe)
            # Keep StateStore.orders remaining in sync with FillSimulator
            # (cancelled orders must not remain fillable; partials reduce size).
            if fill.order_id:
                o = self.state.orders.get(fill.order_id)
                if o is not None:
                    new_sz = o.size - fill.size
                    if new_sz <= 1e-12:
                        self.state.remove_order(fill.order_id)
                    else:
                        from polymaker.domain import OpenOrder, OrderState
                        self.state.upsert_order(OpenOrder(
                            o.order_id, o.token_id, o.side, o.price, new_sz,
                            OrderState.LIVE, o.created_ts,
                        ))
            self._on_fill(fill)
            self._wake_cid(cid)

    def _on_fill(self, fill: Fill) -> None:
        self.risk.note_fill(fill)
        cid = self._token_cid.get(fill.token_id)
        if cid is None:
            return
        est = self.est[cid]
        fv = est.last_fv if est.last_fv is not None else fill.price
        meta = self.metas[cid]
        p = self.profiles.get(cid)
        token_fv = fv if fill.token_id == meta.yes.token_id else (1.0 - fv)
        est.markout.record_fill(fill.side, token_fv, fill.ts)
        # Signed markout proxy: negative = adverse (toxicity)
        markout = -float(getattr(est.markout, "toxicity", 0.0) or 0.0)
        if hasattr(est.markout, "short_term_toxicity"):
            markout = -float(est.markout.short_term_toxicity or 0.0)
        # Judgment: learn from fill edge + markout proxy (toxicity)
        if p is not None and p.use_intelligence:
            tick = max(meta.tick_size, 1e-9)
            # Offset of fill vs FV in ticks (BUY below FV → negative)
            offset = int(round((fill.price - token_fv) / tick))
            edge = (token_fv - fill.price) if fill.side is Side.BUY else (fill.price - token_fv)
            self.intel.record_fill(cid, offset, edge, markout)
        # Feed degradation detector (markout signed: + good for us)
        self.degradation.state_for(cid).record_fill(markout)
        self.degradation.global_state.record_fill(markout)
        pos_yes = self.state.position(meta.yes.token_id)
        pos_no = self.state.position(meta.no.token_id)
        # Track entry time for exit urgency (first BUY that opens inventory)
        if fill.side is Side.BUY:
            pos = pos_yes if fill.token_id == meta.yes.token_id else pos_no
            if pos.size > 0:
                self._pos_entry_ts.setdefault(fill.token_id, fill.ts)
        else:
            pos = pos_yes if fill.token_id == meta.yes.token_id else pos_no
            if pos.size <= 0:
                self._pos_entry_ts.pop(fill.token_id, None)
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

    async def _cancel_market_orders(
        self, cid: str, meta: MarketMeta, *, reason: str = ""
    ) -> None:
        """Pull all resting orders for a market (capital skip / force flat quotes)."""
        live = self.state.orders_for(meta.yes.token_id) + self.state.orders_for(meta.no.token_id)
        if not live:
            return
        oids = [o.order_id for o in live]
        ok = await self.gateway.cancel(oids)
        if ok:
            for o in live:
                if self.paper:
                    self._fill_sim.cancel(o.order_id)
                self.state.remove_order(o.order_id)
            log.info(
                "market_orders_cancelled",
                condition_id=cid,
                n=len(oids),
                reason=reason,
            )
        else:
            await self._refresh_token_orders(meta, grace_s=10.0)

    async def _recompute(self, cid: str) -> None:
        lock = self._locks.get(cid)
        if lock is None:
            return
        async with lock:  # serialize vs the reconcile loop mutating this market
            await self._recompute_locked(cid)

    async def _recompute_locked(self, cid: str) -> None:
        # ── hot path: cache frequently accessed attributes as locals ──
        # This function is called on every book update. Avoiding repeated
        # attribute lookups (meta.x, p.x, self.x) shaves microseconds off
        # the critical path, which compounds across 30+ markets.
        meta = self.metas[cid]
        p = self.profiles[cid]
        yes_token = meta.yes.token_id
        no_token = meta.no.token_id
        tick = meta.tick_size
        yes_book = self.md.book(yes_token)
        if yes_book is None or yes_book.is_empty:
            return

        # crossed/locked or one-sided book -> FV is unreliable; skip this tick.
        yes_view = yes_book.view()
        if yes_view.best_bid is None or yes_view.best_ask is None:
            return
        if yes_view.best_bid >= yes_view.best_ask:
            return

        now = time.time()
        est = self.est[cid]
        est.on_book_view(yes_view, now)
        micro = yes_book.microprice(p.micro_levels)
        if micro is None:
            return
        est.flow.decay_to(now)
        # FV preview for risk marks only — last_fv stays previous until after
        # build_targets so regime jump detection matches the shared pipeline.
        fv = compute_fair_value(micro, est.flow.z, tick, weight=p.flow_fv_weight)

        self.risk.update_mark(yes_token, fv)
        self.risk.update_mark(no_token, 1.0 - fv)

        no_book = self.md.book(no_token)
        pos_yes = self.state.position(yes_token)
        pos_no = self.state.position(no_token)
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
        halted = self.is_quoting_halted(cid)
        llm_paused = cid in self._llm_paused
        blind = market_stale or user_blind or hb_blind or halted
        if blind:
            log.warning("market_blind", condition_id=cid, cid=cid[:8], market_stale=market_stale,
                        user_blind=user_blind, hb_blind=hb_blind, halted=halted,
                        llm_paused=llm_paused)
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

        # Capital-aware reward eligibility: floor size when affordable, else
        # skip this market with an explicit reason (no silent $0-reward quotes).
        typical_px = float(micro) if micro is not None else 0.5
        bankroll = float(self.cfg.risk.bankroll_usdc or 0.0)
        if bankroll <= 0:
            bankroll = float(getattr(p, "bankroll_usdc", 0.0) or 0.0)
        reward_gate = decide_maker_reward_eligibility(
            bankroll_usdc=bankroll,
            rewards_min_size=float(getattr(meta, "rewards_min_size", 0.0) or 0.0),
            exchange_min_shares=float(getattr(meta, "min_order_size", 5.0) or 5.0),
            typical_price=typical_px,
            layers=int(getattr(p, "layers", 1) or 1),
            reward_size_mult=float(getattr(p, "reward_size_mult", 1.0) or 1.0),
            default_base_size_usdc=float(getattr(p, "base_size_usdc", 0.0) or 0.0),
        )
        self._reward_eligibility[cid] = reward_gate
        if reward_gate.skip:
            # Warn once per market, not every requote cycle.
            if not hasattr(self, '_undersized_warned'):
                self._undersized_warned: set[str] = set()
            if cid not in self._undersized_warned:
                self._undersized_warned.add(cid)
                gategood: bool = reward_gate.recommended_base_size_usdc > 0
                gateinfo: str = (f"$reward_min_shortfall={reward_gate.shortfall_pct_pct}%_of_cap"
                    if getattr(reward_gate, "shortfall_pct_pct", None) else "")
                log.info(
                    "capital_info",
                    cid=cid[:8],
                    bankroll_usdc=round(bankroll, 2),
                    rewards_min_size=getattr(meta, "rewards_min_size", 0),
                    reward_eligible=False,
                    quoting_at_exchange_min=not gategood,
                    shortfall=round(reward_gate.required_for_two_sided - reward_gate.bankroll_usdc * 0.95, 2),
                )

        # Scale sizes to DeepSeek's per-market allocation when available,
        # otherwise fall back to global bankroll formula.
        _effective_bankroll = self._discovery_capital.get(cid, 0.0)
        if _effective_bankroll > 0:
            self.cfg.risk.bankroll_usdc = _effective_bankroll
        p = self.risk.cfg.scale_profile_sizes(
            p,
            rewards_min_size=getattr(meta, "rewards_min_size", 0.0),
            typical_price=typical_px,
            exchange_min_shares=float(getattr(meta, "min_order_size", 5.0) or 5.0),
        )
        if _effective_bankroll > 0:
            self.cfg.risk.bankroll_usdc = self._effective_capital  # restore global
        if reward_gate.eligible and reward_gate.recommended_base_size_usdc > 0:
            p = p.model_copy(update={
                "base_size_usdc": max(
                    p.base_size_usdc, reward_gate.recommended_base_size_usdc
                ),
            })

        # Degradation detector: cut size / quarantine / baseline fallback.
        # Quarantine = REDUCE_ONLY (exits still place). Never HALT for quarantine
        # or inventory is trapped with empty targets.
        gs = self.degradation.global_state
        gs.equity = self.risk.equity
        gs.day_start_equity = self.risk.day_start_equity
        # Intel confidence from last decision if present, else 1.0 when off
        intel_conf = 1.0
        if p.use_intelligence:
            st_intel = self.intel.get_state(cid)
            if st_intel.last_decision is not None:
                # Map AS risk + opportunity into [0,1] confidence proxy
                as_r = float(st_intel.last_decision.adverse_selection_risk or 0.0)
                intel_conf = max(0.0, min(1.0, 1.0 - as_r))
            else:
                # Cold start: low sample → moderate confidence
                n_dec = int(st_intel.n_decisions or 0)
                intel_conf = 0.5 if n_dec < 5 else 0.8
        deg = self.degradation.evaluate(cid, intelligence_confidence=intel_conf)
        if deg.halt:
            rd = type(rd)(halt=True, reduce_only=True, size_scale=0.0, reason=deg.reason)
        if deg.quarantine:
            self._quarantined.add(cid)
        quarantined = cid in self._quarantined
        # Quarantine → reduce_only (entries off, exits on). Size scale stays
        # 1.0 so exit legs are not zeroed; non-quarantine applies deg cut.
        size_scale = rd.size_scale * (1.0 if quarantined else float(deg.size_multiplier))
        # ── DeepSeek trading authority: per-market aggression ──
        _grok_agg = float(self._grok_aggression.get(cid, 1.0))
        if abs(_grok_agg - 1.0) > 0.005:
            size_scale *= _grok_agg
        # ──────────────────────────────────────────────────────
        use_intel = p.use_intelligence and not deg.use_baseline_profile and not quarantined

        # Shared decision pipeline (same as replay): regime + intel + quotes.
        n_trades_1h = len(self._trade_ts.get(cid, []))
        last_trade = max(self._trade_ts[cid]) if n_trades_1h else 0.0
        secs_stale = (now - last_trade) if last_trade > 0 else (
            0.0 if n_trades_1h == 0 and meta.rewards_daily_rate > 0 else 120.0
        )
        if n_trades_1h == 0 and meta.rewards_daily_rate > 0:
            secs_stale = 0.0
        self._last_book_ts[cid] = now

        # Exit urgency: same formula as replay (hold time + toxicity bump)
        def _urgency(token_id: str, size: float) -> float:
            if size <= 0:
                return 0.0
            t0 = self._pos_entry_ts.get(token_id, now)
            hold = max(0.0, now - t0)
            base = min(1.0, hold / max(p.exit_urgency_s, 1.0))
            tox = float(getattr(est.markout, "toxicity", 0.0) or 0.0)
            if tox > 0.02:
                base = min(1.0, base + 0.35)
            return base

        # ── DeepSeek automated triggers: evaluate 24/7, zero API cost ──
        # DeepSeek set these on the 10-min oversight cycle. They fire
        # sub-second here on every requote without calling DeepSeek.
        if self._deepseek_triggers:
            from polymaker.intelligence.deepseek_triggers import evaluate_triggers
            snap = self._oversight_snapshot()
            violations = evaluate_triggers(self._deepseek_triggers, snap)
            for v in violations:
                log.warning("llm_trigger_fired", **v.as_dict())
                self._apply_trigger_action(v)
        # ────────────────────────────────────────────────────────

        pipe = build_targets(
            meta=meta,
            profile=p,
            yes_view=yes_view,
            no_view=(no_book.view() if no_book else BookView(None, 0.0, None, 0.0, None, None, 0.0, 0.0)),
            pos_yes=pos_yes,
            pos_no=pos_no,
            est=est,
            regime_machine=self.regime_m[cid],
            now=now,
            micro=micro,
            risk_size_scale=size_scale,
            # Quarantine must NOT set risk_halt — that empties exits.
            risk_halt=rd.halt,
            risk_reduce_only=rd.reduce_only or quarantined or deg.quarantine,
            hours_to_end=hours_to_end,
            sweep_flagged=self._sweep.pop(cid, False),
            ws_stale=ws_stale,
            market_resolved=self.is_quoting_halted(cid),
            intel=self.intel if use_intel else None,
            n_trades_last_hour=n_trades_1h,
            seconds_since_last_trade=secs_stale,
            yes_exit_urgency=_urgency(yes_token, pos_yes.size),
            no_exit_urgency=_urgency(no_token, pos_no.size),
        )
        if pipe is None:
            return
        tq = pipe.targets
        fv = pipe.fv
        regime = pipe.regime
        attr = pipe.attribution
        est.on_fair_value(fv, now)
        self.risk.update_mark(yes_token, fv)
        self.risk.update_mark(no_token, 1.0 - fv)

        intel_skip = attr.intelligence_decision == "SKIP"
        intel_size = attr.size_multiplier
        intel_band_frac = attr.buy_band_frac
        intel_spread_mult = attr.spread_multiplier
        intel_buy_offset = attr.buy_offset_ticks
        intel_reason = attr.intel_reason

        # ── V3 governance override: apply oversight-driven spread multiplier ──
        gov_spread_mult = self._per_market_spread_mult.get(cid, 1.0)
        if abs(gov_spread_mult - 1.0) > 0.005:
            intel_spread_mult *= gov_spread_mult
            intel_spread_mult = max(0.5, min(3.0, intel_spread_mult))
            intel_reason = f"{intel_reason}+gov_x{round(gov_spread_mult, 2)}"
        # ─────────────────────────────────────────────────────────────

        # ── DeepSeek band override: DeepSeek says 'go_aggressive/defensive' ──
        _grok_band = self._grok_band_override.get(cid)
        if _grok_band is not None and isinstance(intel_band_frac, (int, float)):
            intel_band_frac = float(_grok_band)
            intel_reason = f"{intel_reason}+grok_band={round(_grok_band, 2)}"
        # ─────────────────────────────────────────────────────────────

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
            intel_skip=intel_skip,
            intel_size=intel_size,
            intel_band_frac=intel_band_frac if intel_band_frac is not None else -1.0,
            intel_buy_offset=intel_buy_offset if intel_buy_offset is not None else 0,
            intel_spread_mult=intel_spread_mult,
            intel_reason=intel_reason[:120] if intel_reason else "",
            intel_decision=attr.intelligence_decision,
            reason_codes=",".join(attr.reason_codes),
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
                ok_place = len(placed) == len(plan.to_place)
                self.risk.note_order_result(ok_place)
                # Degradation paths: fill-rate + order-error rate
                self.degradation.global_state.record_order_result(ok_place)
                self.degradation.state_for(cid).record_order_result(ok_place)
                reward_band = meta.rewards_max_spread / 100.0
                for o in placed:
                    self.state.upsert_order(o)
                    if self.paper:
                        self._fill_sim.place(o)
                    self.degradation.state_for(cid).record_quote()
                    self.degradation.global_state.record_quote()
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
        self._last_regime[cid] = regime.value
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
                for _cid, meta in self.metas.items():
                    lock = self._locks.get(_cid)
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
                for _cid, meta in self.metas.items():
                    lock = self._locks.get(_cid)
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
            # Accepting again: clear Gamma halt only. Never clear ops/LLM pause
            # (``_llm_paused``) — that would wipe oversight pause_market.
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

    def emit_aspirational_vs_honest(
        self,
        *,
        honest: Any | None = None,
        bankroll_usdc: float | None = None,
    ) -> dict[str, Any]:
        """Emit aspirational 10–15% target vs honest realized metrics.

        Pure compare + metrics log. Monopoly diagnostic is never the sole PASS.
        """
        from polymaker.metrics.honest_pnl import (
            HonestPnL,
            compare_aspirational_vs_honest,
            compute_honest_pnl,
        )

        b = float(
            bankroll_usdc
            if bankroll_usdc is not None
            else (self.cfg.risk.bankroll_usdc or 0.0)
        )
        if honest is None:
            # Live path: daily equity change as without-rewards proxy until
            # a full metrics report is attached offline.
            daily = float(self.risk.daily_pnl)
            honest = compute_honest_pnl(
                instant_spread_usdc=daily,
                markout_30s_mean=0.0,
                markout_n=0,
                n_fill=0,
                n_quote=0,
                rewards_daily_rate=0.0,
                eligible_in_band_seconds=0.0,
            )
        elif not isinstance(honest, HonestPnL):
            honest = HonestPnL(**{
                k: honest[k]
                for k in (
                    "instant_spread_usdc",
                    "as_adjusted_spread_usdc",
                    "pnl_without_rewards_usdc",
                    "pnl_conservative_usdc",
                    "pnl_base_usdc",
                    "pnl_optimistic_usdc",
                    "pnl_monopoly_diagnostic_usdc",
                    "financial_claim_ok",
                    "n_fill",
                    "n_quote",
                )
                if isinstance(honest, dict) and k in honest
            }) if isinstance(honest, dict) else honest

        cmp_ = compare_aspirational_vs_honest(
            bankroll_usdc=b,
            honest=honest,
            aspirational_low=float(self.cfg.engine.aspirational_daily_return_low),
            aspirational_high=float(self.cfg.engine.aspirational_daily_return_high),
        )
        payload = cmp_.as_dict()
        self.metrics.emit("aspirational_vs_honest", **payload)
        return payload

    def emit_share_adjusted_planning(
        self,
        *,
        bankroll_usdc: float | None = None,
        alt_bankrolls: tuple[float, ...] | None = None,
    ) -> dict[str, Any]:
        """Emit share-adjusted expected rewards for live markets (headline KPI).

        Monopoly diagnostic is included per market but never used as the rank
        key. Capital-tight scenarios appear as skip with explicit reason.
        """
        from polymaker.strategy.share_planning import (
            plan_capital_scenarios,
            plan_share_adjusted,
        )

        b = float(
            bankroll_usdc
            if bankroll_usdc is not None
            else (self.cfg.risk.bankroll_usdc or self._effective_capital or 0.0)
        )
        alts = alt_bankrolls or (30.0, max(b, 30.0), max(b * 5.0, 2000.0))
        markets_out: list[dict[str, Any]] = []
        for cid, meta in self.metas.items():
            mid = 0.5
            if meta.best_bid > 0 and meta.best_ask > 0:
                mid = 0.5 * (meta.best_bid + meta.best_ask)
            plan = plan_share_adjusted(
                bankroll_usdc=b,
                rewards_daily_rate=float(meta.rewards_daily_rate or 0.0),
                rewards_min_size=float(meta.rewards_min_size or 0.0),
                market_liquidity=float(meta.liquidity_num or 0.0),
                typical_price=mid,
                exchange_min_shares=float(meta.min_order_size or 5.0),
                condition_id=cid,
            )
            scen = plan_capital_scenarios(
                rewards_daily_rate=float(meta.rewards_daily_rate or 0.0),
                rewards_min_size=float(meta.rewards_min_size or 0.0),
                market_liquidity=float(meta.liquidity_num or 0.0),
                typical_price=mid,
                bankrolls=alts,
                condition_id=cid,
            )
            row = {
                **plan.as_dict(),
                "slug": meta.slug,
                "scenarios": [s.as_dict() for s in scen.scenarios],
            }
            markets_out.append(row)
            self.metrics.emit(
                "share_adjusted_plan",
                condition_id=cid,
                headline_kpi="share_adjusted_expected_usdc",
                share_adjusted_expected_usdc=plan.share_adjusted_expected_usdc,
                estimated_share_of_pool=plan.estimated_share_of_pool,
                monopoly_diagnostic_usdc=plan.monopoly_diagnostic_usdc,
                skip=plan.skip,
                quote_size_usdc=plan.quote_size_usdc,
                bankroll_usdc=b,
            )
        markets_out.sort(
            key=lambda r: float(r.get("share_adjusted_expected_usdc") or 0.0),
            reverse=True,
        )
        # Multi-market portfolio + capacity curve (best book for this capital)
        mm_payload = self.emit_multi_market_dominator(bankroll_usdc=b)

        payload: dict[str, Any] = {
            "headline_kpi": "share_adjusted_expected_usdc",
            "bankroll_usdc": b,
            "n_markets": len(markets_out),
            "markets": markets_out,
            "portfolio": mm_payload.get("portfolio"),
            "capacity_curve": mm_payload.get("capacity_curve"),
            "note": (
                "Dominate by raising share_of_pool × n_markets × uptime; "
                "monopoly is ceiling only. %/day decays as capital outgrows pools."
            ),
        }
        self.metrics.emit(
            "share_adjusted_planning",
            headline_kpi=payload["headline_kpi"],
            bankroll_usdc=b,
            n_markets=len(markets_out),
            capital_outgrew=bool(
                (mm_payload.get("capacity_curve") or {}).get(
                    "capital_outgrew_reward_surface"
                )
            ),
        )
        return payload

    def emit_multi_market_dominator(
        self,
        *,
        bankroll_usdc: float | None = None,
        candidate_markets: list[dict[str, Any]] | None = None,
        max_markets: int | None = None,
    ) -> dict[str, Any]:
        """Best multi-market portfolio + capacity curve for this bankroll.

        No hard limit on universe size — only simultaneous slots
        (``auto_discovery_max_markets``) and concentration. Emits metrics for
        operators: total share-adj $, %/day, and capital_outgrew flag.
        """
        from polymaker.strategy.share_planning import (
            build_dominator_operator_report,
            capacity_curve,
            optimize_multi_market_portfolio,
            recommend_max_markets,
        )

        b = float(
            bankroll_usdc
            if bankroll_usdc is not None
            else (self.cfg.risk.bankroll_usdc or self._effective_capital or 0.0)
        )
        hard_cap = int(self.cfg.engine.auto_discovery_max_markets or 20)
        conc = float(self.cfg.risk.max_market_concentration_pct or 0.4)
        deploy = float(getattr(self.cfg.risk, "capital_deploy_frac", 0.6) or 0.6)
        horizon = float(getattr(self.cfg.risk, "prefer_horizon_days", 14.0) or 0.0)

        if candidate_markets is not None:
            cands = list(candidate_markets)
        else:
            cands = []
            for cid, meta in self.metas.items():
                mid = 0.5
                if meta.best_bid > 0 and meta.best_ask > 0:
                    mid = 0.5 * (meta.best_bid + meta.best_ask)
                cands.append({
                    "condition_id": cid,
                    "rewards_daily_rate": float(meta.rewards_daily_rate or 0.0),
                    "rewards_min_size": float(meta.rewards_min_size or 0.0),
                    "liquidity_num": float(meta.liquidity_num or 0.0),
                    "typical_price": mid,
                    "min_order_size": float(meta.min_order_size or 5.0),
                    "rewards_max_spread": float(meta.rewards_max_spread or 0.0),
                    "end_date_iso": getattr(meta, "end_date_iso", None),
                    "slug": meta.slug,
                })
            # Also merge catalog top so capacity uses broader surface
            try:
                for row in self.catalog.top(limit=80):
                    meta = row[0] if isinstance(row, tuple) else row
                    cid = getattr(meta, "condition_id", "") or ""
                    if not cid or any(c["condition_id"] == cid for c in cands):
                        continue
                    mid = 0.5
                    bb = float(getattr(meta, "best_bid", 0) or 0)
                    ba = float(getattr(meta, "best_ask", 0) or 0)
                    if bb > 0 and ba > 0:
                        mid = 0.5 * (bb + ba)
                    cands.append({
                        "condition_id": cid,
                        "rewards_daily_rate": float(getattr(meta, "rewards_daily_rate", 0) or 0),
                        "rewards_min_size": float(getattr(meta, "rewards_min_size", 0) or 0),
                        "liquidity_num": float(getattr(meta, "liquidity_num", 0) or 0),
                        "typical_price": mid,
                        "min_order_size": float(getattr(meta, "min_order_size", 5) or 5),
                        "rewards_max_spread": float(getattr(meta, "rewards_max_spread", 0) or 0),
                        "end_date_iso": getattr(meta, "end_date_iso", None),
                        "slug": getattr(meta, "slug", ""),
                    })
            except Exception:  # noqa: BLE001
                pass

        # Partial deploy + multi-market small slices (not full dump)
        if max_markets is not None:
            max_m = min(int(max_markets), hard_cap)
        else:
            max_m = recommend_max_markets(
                cands,
                bankroll_usdc=b,
                hard_cap=hard_cap,
                max_concentration=conc,
                capital_deploy_frac=deploy,
                prefer_horizon_days=horizon,
            )
        port = optimize_multi_market_portfolio(
            cands,
            bankroll_usdc=b,
            max_markets=max_m,
            max_concentration=conc,
            auto_max_markets=False,
            hard_cap_markets=hard_cap,
            capital_deploy_frac=deploy,
            prefer_horizon_days=horizon,
        )
        curve = capacity_curve(
            cands,
            bankrolls=(100.0, 200.0, 300.0, 500.0, 1000.0, 2000.0, 5000.0),
            current_bankroll=b,
            max_markets=max_m,
            max_concentration=conc,
            capital_deploy_frac=deploy,
            prefer_horizon_days=horizon,
        )
        # Stash preferred discovery capital from portfolio picks
        for p in port.picks:
            self._discovery_capital[p.condition_id] = p.allocated_usdc

        port_d = port.as_dict()
        curve_d = curve.as_dict()
        operator = build_dominator_operator_report(port, curve)
        self.metrics.emit(
            "multi_market_portfolio",
            bankroll_usdc=b,
            n_markets=port.n_markets,
            total_share_adjusted_usdc=port.total_share_adjusted_usdc,
            total_risk_adjusted_usdc=port.total_risk_adjusted_usdc,
            daily_return_pct=port.daily_return_pct,
            capital_outgrew=curve.capital_outgrew_reward_surface,
            peak_pct=curve.peak_pct,
            max_markets_recommended=port.max_markets_recommended,
        )
        if curve.capital_outgrew_reward_surface:
            log.info(
                "capital_outgrew_reward_surface",
                bankroll_usdc=b,
                current_pct=round(curve.current_pct, 6),
                peak_pct=round(curve.peak_pct, 6),
                peak_bankroll=curve.peak_pct_bankroll,
                reason=curve.outgrew_reason,
                operator_message=operator.get("operator_message", "")[:200],
            )
        return {
            "portfolio": port_d,
            "capacity_curve": curve_d,
            "operator": operator,
            "headline_kpi": "total_risk_adjusted_usdc",
            "n_candidates": len(cands),
            "max_markets_recommended": max_m,
        }

    async def _maintenance_loop(self) -> None:
        """Periodic REST book refresh + auto-compounding."""
        while self._running:
            await asyncio.sleep(120.0)
            for meta in list(self.metas.values()):
                for tok in (meta.yes.token_id, meta.no.token_id):
                    with contextlib.suppress(Exception):
                        await self._refresh_book(tok)
            with contextlib.suppress(Exception):
                self.emit_aspirational_vs_honest()
            with contextlib.suppress(Exception):
                self.emit_share_adjusted_planning()
            # ── Auto compounding: update effective bankroll from PnL ──
            if self._day_start_equity > 0 and self._base_capital > 0:
                try:
                    equity = self.risk.equity
                    growth = equity / max(self._day_start_equity, 0.01)
                    # Only compound up (never shrink from compounding).
                    if growth > 1.01:  # 1%+ growth = compound
                        new_cap = min(
                            self._base_capital * 100,  # hard ceiling: 100× base
                            self._effective_capital * growth
                        )
                        if new_cap > self._effective_capital * 1.005:
                            self._effective_capital = new_cap
                            self.cfg.risk.bankroll_usdc = self._effective_capital
                            log.info("compounding",
                                     growth=round(growth, 4),
                                     effective_capital=round(self._effective_capital, 2))
                except Exception:
                    pass
            # ──────────────────────────────────────────────────────

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


class _GovernedJsonFacade:
    """DeepSeekAgent-compatible ``chat_json_tool`` that always hits LLMGovernance.

    OversightLoop and MarketDiscovery call ``(args, resp) = agent.chat_json_tool(...)``.
    :class:`GovernedDeepSeekAgent` returns a :class:`GovernedResponse` instead, so we
    wrap the raw agent + governance here and keep that call shape while ensuring
    every structured LLM response is audited via ``check_and_log`` (AC2).
    """

    def __init__(self, agent: Any, governance: LLMGovernance) -> None:
        self._agent = agent
        self._gov = governance
        self.model = getattr(agent, "model", "governed")
        self.last_decision: Any = None

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return await self._agent.chat(messages, **kwargs)

    async def chat_json_tool(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[Any, Any]:
        started = time.time()
        parsed, resp = await self._agent.chat_json_tool(messages, **kwargs)

        # Flatten nested oversight actions for direction/size scans.
        flat: dict[str, Any] = {}
        if isinstance(parsed, dict):
            flat = {k: v for k, v in parsed.items() if k != "actions"}
            nested = parsed.get("actions")
            if isinstance(nested, list):
                for item in nested:
                    if not isinstance(item, dict):
                        continue
                    # Hoist forbidden direction fields if present
                    for bad in ("side", "direction", "buy_this_market"):
                        if bad in item:
                            flat[bad] = item[bad]
                    params = item.get("params") or {}
                    if isinstance(params, dict):
                        if "size_pct" in params:
                            flat["size_pct"] = params["size_pct"]
                        if "spread_mult" in params:
                            flat["spread_mult"] = params["spread_mult"]
                        if "mult" in params and "spread_mult" not in flat:
                            flat["spread_mult"] = params["mult"]
            elif isinstance(nested, dict):
                flat.update(nested)
            # Discovery rankings may carry suggested_size_pct
            for item in parsed.get("rankings") or []:
                if isinstance(item, dict) and "suggested_size_pct" in item:
                    flat["size_pct"] = item.get("suggested_size_pct")
                    break

        prompt_parts = []
        for m in messages:
            prompt_parts.append(f"[{m.get('role', '')}] {m.get('content', '')}")
        decision = self._gov.check_and_log(
            prompt="\n".join(prompt_parts),
            response={"actions": flat} if flat else {"content": str(parsed)},
            llm_started_at=started,
            context={"kind": kwargs.get("kind") or "tool"},
            confidence=float(
                (parsed or {}).get("confidence", 0.5)
                if isinstance(parsed, dict)
                else 0.5
            ),
        )
        self.last_decision = decision

        # Strip directional nested actions when governance rejects.
        if (
            not decision.approved
            and isinstance(parsed, dict)
            and "directional" in (decision.rejection_reason or "")
        ):
            if isinstance(parsed.get("actions"), list):
                parsed = {
                    **parsed,
                    "actions": [
                        {
                            "type": "no_op",
                            "dry_run": False,
                            "reason": "governance_rejected_direction",
                        }
                    ],
                }
            else:
                parsed = {"narrative": parsed.get("narrative", ""), "actions": []}
        return parsed, resp

