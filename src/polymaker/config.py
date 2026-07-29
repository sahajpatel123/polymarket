"""Configuration: pydantic models over local TOML files + .env secrets.

Replaces the v1 Google Sheets config entirely. Three files under config/:
  config.toml    engine/wallet/risk/execution settings
  strategy.toml  named parameter profiles
  markets.toml   the trade list (market -> profile + overrides)

Secrets (private key, wallet address) come only from the environment / .env.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WalletConfig(BaseModel):
    chain_id: int = 137
    signature_type: int = 2
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    polygon_rpc: str = "https://polygon-bor-rpc.publicnode.com"


class EngineConfig(BaseModel):
    debounce_ms: int = 200
    # baseline periodic re-quote (book reactions are event-driven & instant; this
    # is just a slow refresh for cool-off re-entry / exit-urgency updates). A
    # precise wake is also scheduled for the exact moment an EVENT cool-off ends.
    quoter_tick_s: float = 60.0
    reconcile_interval_s: float = 30.0
    catalog_refresh_s: float = 900.0
    heartbeat: bool = True
    heartbeat_interval_s: float = 5.0
    journal: bool = True
    loop: str = "uvloop"
    # ── auto market discovery ──
    # When enabled, the engine periodically re-scans Gamma for new markets that
    # pass the score filter and auto-adds them to the live trade list. No
    # manual `markets-add` needed — the bot finds and trades new markets on
    # its own. When disabled, the engine only trades markets listed in
    # markets.toml at startup (legacy behavior).
    auto_discovery_enabled: bool = False
    # Seconds between auto-discovery scans. 3600 = hourly, 900 = every 15 min.
    auto_discovery_interval_s: float = 3600.0
    # Gamma tag slugs to scan (e.g. ("politics", "sports", "crypto")).
    auto_discovery_tags: tuple[str, ...] = ("politics",)
    # Minimum scanner score to auto-add a market (filters junk).
    auto_discovery_min_score: float = 0.01
    # Maximum number of auto-discovered markets to trade simultaneously.
    # Hard cap to prevent resource exhaustion from market spam.
    auto_discovery_max_markets: int = 20
    # Profile to assign to auto-discovered markets.
    auto_discovery_profile: str = "political-longdated"
    # Hot-reload markets.toml when it changes on disk (via watchfiles).
    auto_discovery_hot_reload: bool = True
    # Profitability gates for auto-discovery (fix #7: add unprofitable markets)
    auto_discovery_min_liquidity: float = 10000.0
    auto_discovery_min_daily_rate: float = 10.0
    auto_discovery_max_spread_cents: float = 5.0
    # Aspirational daily return target band (fraction of bankroll). Tracked vs
    # honest realized PnL only — never a guarantee and never monopoly-PASS.
    aspirational_daily_return_low: float = 0.10   # 10%
    aspirational_daily_return_high: float = 0.15  # 15%
    # ── live operator dashboard (localhost) ──
    # Opens a multi-layout UI in the browser when the engine starts.
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_open_browser: bool = True


class RiskConfig(BaseModel):
    """Risk limits. Prefer setting `bankroll_usdc` once — all USDC caps scale.

    When ``bankroll_usdc > 0``, absolute caps are *derived* from the fraction
    fields (total_exposure_frac, market_notional_frac, …). That way a $30
    paper wallet and a $5_000 live wallet share one risk policy.

    When ``bankroll_usdc == 0``, the absolute USDC fields are used as-is
    (legacy / explicit override mode).
    """

    # ── single capital knob (set this to your real balance) ─────────────
    bankroll_usdc: float = 0.0

    # ── fractions of bankroll (used when bankroll_usdc > 0) ─────────────
    total_exposure_frac: float = 1.0       # can deploy up to 100% of bankroll
    market_notional_frac: float = 0.35     # max ~35% in one market
    event_group_frac: float = 0.50         # max 50% in one neg-risk event group
    daily_loss_frac: float = 0.10          # kill at −10% day
    market_loss_frac: float = 0.05         # reduce-only a market at −5% of bankroll
    # Max fraction of **full bankroll** in one market (portfolio slice + risk).
    # 0.25 @ $50 ⇒ ≤$12.5 per event — simultaneous small books, not one dump.
    max_market_concentration_pct: float = 0.25
    # Portfolio deploy policy (multi-market small slices — not dump-all):
    # Only this fraction of bankroll is eligible for live quoting; the rest
    # is reserve. Example $50 × 0.60 = $30 working capital across markets.
    capital_deploy_frac: float = 0.60
    # Soft preference: boost markets ending within this many days (0 = off).
    # ~14d matches “events over the next two weeks.”
    prefer_horizon_days: float = 14.0
    # Gas circuit breaker: halt if cumulative gas ≥ this fraction of bankroll
    # (or of day-start equity / total-exposure fallback).
    max_gas_cost_pct: float = 0.10

    # ── absolute USDC caps (legacy mode, or filled by resolve_from_bankroll) ─
    max_total_exposure_usdc: float = 5000.0
    max_event_group_loss_usdc: float = 1000.0
    max_market_notional_usdc: float = 800.0
    daily_loss_kill_usdc: float = 250.0
    max_market_loss_usdc: float = 50.0

    ws_stale_halt_s: float = 10.0
    # user WS down this long -> we can't see our fills -> pull all quotes
    user_ws_blind_halt_s: float = 15.0
    # consecutive heartbeat failures -> exchange is auto-cancelling us -> halt
    heartbeat_halt_failures: int = 3
    max_order_error_rate: float = 0.25

    def resolve_from_bankroll(self) -> RiskConfig:
        """Return a copy with absolute caps derived from bankroll when set.

        Idempotent: if bankroll is 0, returns self unchanged. Always recomputes
        from fractions when bankroll > 0 so config reloads stay consistent.
        """
        b = float(self.bankroll_usdc)
        if b <= 0:
            return self
        data = self.model_dump()
        data["max_total_exposure_usdc"] = b * float(self.total_exposure_frac)
        data["max_market_notional_usdc"] = b * float(self.market_notional_frac)
        data["max_event_group_loss_usdc"] = b * float(self.event_group_frac)
        data["daily_loss_kill_usdc"] = b * float(self.daily_loss_frac)
        data["max_market_loss_usdc"] = b * float(self.market_loss_frac)
        # Concentration hard-cap also respects market_notional_frac.
        conc = min(
            data["max_market_notional_usdc"],
            data["max_total_exposure_usdc"] * float(self.max_market_concentration_pct),
        )
        data["max_market_notional_usdc"] = conc
        return RiskConfig(**data)

    def scale_profile_sizes(
        self,
        profile: StrategyProfile,
        *,
        rewards_min_size: float = 0.0,
        typical_price: float = 0.5,
        exchange_min_shares: float = 5.0,
    ) -> StrategyProfile:
        """Scale a profile's base_size / q_max / bankroll to this risk bankroll.

        Used so a single advanced profile works at $30 or $5_000 without
        hand-editing every size knob. No-op when bankroll is unset.

        When ``rewards_min_size > 0`` and capital can fund a reward-eligible
        two-sided cycle, base_size is floored via
        :func:`decide_maker_reward_eligibility`. When capital cannot fund
        the floor, sizes are left at the bankroll heuristic — the engine
        must call the same gate and **skip** the market rather than quote
        undersized for $0 rewards.
        """
        from polymaker.benchmark.capital import decide_maker_reward_eligibility

        b = float(self.bankroll_usdc)
        if b <= 0:
            return profile
        gate = decide_maker_reward_eligibility(
            bankroll_usdc=b,
            rewards_min_size=float(rewards_min_size or 0.0),
            exchange_min_shares=float(exchange_min_shares or 5.0),
            typical_price=float(typical_price or 0.5),
            layers=int(getattr(profile, "layers", 1) or 1),
            reward_size_mult=float(getattr(profile, "reward_size_mult", 1.0) or 1.0),
            default_base_size_usdc=float(profile.base_size_usdc or 0.0),
        )
        if gate.eligible and gate.recommended_base_size_usdc > 0:
            base = gate.recommended_base_size_usdc
        else:
            # Unset eligibility (no min) or skip-path: bankroll heuristic only.
            base = max(2.0, min(250.0, b * 0.10))
        q_max = max(base, float(self.max_market_notional_usdc))
        return profile.model_copy(update={
            "base_size_usdc": base,
            "q_max_usdc": q_max,
            "bankroll_usdc": b,
        })


class ExecutionConfig(BaseModel):
    rate_budget_fraction: float = 0.25
    post_only: bool = True
    max_orders_per_batch: int = 15


class PathsConfig(BaseModel):
    db: str = "state.db"
    journal_dir: str = "journal"
    log_dir: str = "logs"


class StrategyProfile(BaseModel):
    """One named parameter set. Every knob the quoter uses lives here."""

    model_config = ConfigDict(extra="forbid")

    # fair value
    micro_levels: int = 3
    flow_ewma_halflife_s: float = 120.0
    # Weight on flow_z * tick added to microprice in compute_fair_value.
    # Default 0.5 preserves historical behavior; 0 disables the flow nudge.
    flow_fv_weight: float = 0.5
    # spread / skew
    gamma: float = 0.5
    delta_min_ticks: int = 2
    c_vol: float = 1.2
    c_tox: float = 2.0
    # Weight on Kyle λ adverse-selection half-spread add-on (price units).
    # Default 0 keeps quotes unchanged; >0 widens δ by c_kyle * 2 * λ * size_proxy.
    c_kyle: float = 0.0
    # vol horizons
    vol_short_halflife_s: float = 10.0
    vol_long_halflife_s: float = 900.0
    # sizing / inventory
    base_size_usdc: float = 50.0
    q_max_usdc: float = 500.0
    q_soft_frac: float = 0.6
    layers: int = 2
    layer_step_ticks: int = 2
    # multiplier on the market's reward min-size that reward-eligible orders are
    # bumped to (margin above the scoring floor). 1.5 => 100-share min -> 150.
    reward_size_mult: float = 1.0
    # placement / churn
    reprice_ticks: int = 2
    resize_frac: float = 0.15
    min_edge_ticks: int = 1
    # When True, improve BUY bids up to the book best bid (join touch) if the
    # touch is still ≤ FV−min_edge and within the reward join distance.
    # Default False preserves sit-behind / band-floor farming behavior.
    join_best_bid: bool = False
    # regime
    event_cooloff_s: float = 60.0
    event_jump_ticks: int = 8
    # sweep = a print >= event_sweep_mult order-sizes AND >= event_sweep_frac of
    # the near-touch depth it consumed (both must hold to flag a toxic sweep)
    event_sweep_mult: float = 4.0
    event_sweep_frac: float = 0.8
    trend_flow_z: float = 1.5
    # short/long realized-vol ratio that trips TRENDING (half size). On a thin
    # book microprice jitter inflates this without real trade flow, so raise it
    # for reward-farming markets that trade rarely.
    trend_vol_ratio: float = 2.0
    # lifecycle
    reduce_only_hours: float = 24.0
    halt_before_hours: float = 2.0
    # TOML-compat unused by live path (C-04) — kept so strategy.toml loads.
    end_date_taper_days: float = 7.0
    # exits
    merge_min_size: float = 20.0
    # TOML-compat unused by live path (C-04) — engine never maps hold-time → urgency.
    exit_urgency_s: float = 900.0
    # ── order book safety ──
    # Max open orders per market (per side). Prevents order book accumulation
    # when the strategy requotes on every book change. With layers=3 and
    # max_open_orders_per_market=2, each side has at most 2 orders = 4 total
    # per market. Set to 0 to disable.
    max_open_orders_per_market: int = 0
    # ── advanced quoting (Tier 2 opt-in) ──
    # When True, the engine uses the Avellaneda-Stoikov optimal pricing
    # model + Kelly-inspired sizing instead of the simple linear skew.
    # See strategy/advanced_quoting.py and docs/ADVANCED_QUOTING.md.
    # Default: False (use simple model for safety until backtested).
    use_advanced_quoting: bool = False
    # Total bankroll for Kelly sizing (USD). When 0, uses profile base_size.
    # For a $30 paper account, set this to 30.0.
    bankroll_usdc: float = 0.0
    # Fraction of full Kelly to deploy (0.25 = quarter-Kelly). Only used when
    # use_advanced_quoting is True. Default matches prior hard-coded value.
    kelly_fraction: float = 0.25
    # ── intelligence / judgment layer ──
    # When True, DecisionFramework gates quoting (skip dead/stale/toxic),
    # scales size, and chooses buy_band_frac from regime + fill learning.
    # Default False preserves blind in-band farming.
    use_intelligence: bool = False
    # Ablation mode when use_intelligence is True:
    #   "full"      — skip + size + band + offsets (default)
    #   "gate_only" — only skip dead/stale; no size/band learning
    #   "off"       — force intelligence off even if flag True
    intelligence_mode: str = "full"

    def with_overrides(self, overrides: dict[str, Any]) -> StrategyProfile:
        """Return a copy with per-market override values applied."""
        if not overrides:
            return self
        data = self.model_dump()
        for k, v in overrides.items():
            if k in data:
                data[k] = v
        return StrategyProfile(**data)


# Keys allowed on a market entry that are NOT profile overrides.
_MARKET_RESERVED = {"slug", "condition_id", "profile", "enabled", "category"}


class MarketEntry(BaseModel):
    """One line of the trade list. Extra keys are treated as profile overrides."""

    model_config = ConfigDict(extra="allow")

    slug: str | None = None
    condition_id: str | None = None
    profile: str = "political-longdated"
    enabled: bool = True
    # Category tag (politics/sports/crypto/news/...) for multi-category scanning.
    # Inferred from Gamma tag at scan time if not set here.
    category: str | None = None

    @model_validator(mode="after")
    def _need_identifier(self) -> MarketEntry:
        if not self.slug and not self.condition_id:
            raise ValueError("market entry needs a slug or condition_id")
        return self

    @property
    def overrides(self) -> dict[str, Any]:
        extra = self.model_extra or {}
        return {k: v for k, v in extra.items() if k not in _MARKET_RESERVED and k != "category"}

    @property
    def ref(self) -> str:
        return self.slug or self.condition_id or "?"


class Secrets(BaseSettings):
    """Loaded from environment / .env. Never written to disk by us."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    pk: str = Field(default="", alias="PK")
    browser_address: str = Field(default="", alias="BROWSER_ADDRESS")
    polygon_rpc: str | None = Field(default=None, alias="POLYGON_RPC")
    alert_webhook_url: str | None = Field(default=None, alias="ALERT_WEBHOOK_URL")
    # xAI Grok (optional) — enables governed LLM discovery / oversight on the
    # live path. Empty → deterministic-only fallback (no crash).
    xai_api_key: str = Field(default="", alias="XAI_API_KEY")
    # Polymarket builder API creds (self-generated via L2 auth: clob.create_builder_api_key)
    # + relayer URL — needed to merge a V2 DepositWallet (sig_type 1/3), whose execute()
    # only accepts calls from its factory/relayer. See merge.py.
    builder_key: str | None = Field(default=None, alias="POLY_BUILDER_KEY")
    builder_secret: str | None = Field(default=None, alias="POLY_BUILDER_SECRET")
    builder_passphrase: str | None = Field(default=None, alias="POLY_BUILDER_PASSPHRASE")
    relayer_url: str = Field(default="https://relayer-v2.polymarket.com", alias="POLY_RELAYER_URL")

    @property
    def has_wallet(self) -> bool:
        return bool(self.pk and self.browser_address)

    @property
    def has_builder_creds(self) -> bool:
        return bool(self.builder_key and self.builder_secret and self.builder_passphrase)

    @property
    def has_xai(self) -> bool:
        return bool(self.xai_api_key and self.xai_api_key.strip())


class Config(BaseModel):
    """Fully-resolved configuration tree."""

    wallet: WalletConfig = WalletConfig()
    engine: EngineConfig = EngineConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    paths: PathsConfig = PathsConfig()
    profiles: dict[str, StrategyProfile] = {}
    markets: list[MarketEntry] = []
    secrets: Secrets = Field(default_factory=Secrets)
    config_dir: Path = Path("config")

    @property
    def proxy(self) -> str | None:
        # Standard proxy env var; ALL_PROXY lets you route through an SSH tunnel
        # (e.g. simulate colocation during local testing). httpx and web3 honor
        # it automatically once load_dotenv() has run.
        return os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY")

    @property
    def enabled_markets(self) -> list[MarketEntry]:
        return [m for m in self.markets if m.enabled]

    def profile_for(self, entry: MarketEntry) -> StrategyProfile:
        base = self.profiles.get(entry.profile)
        if base is None:
            raise KeyError(f"unknown strategy profile: {entry.profile!r}")
        return base.with_overrides(entry.overrides)

    @classmethod
    def load(cls, config_dir: str | Path = "config", *, load_env: bool = True) -> Config:
        cdir = Path(config_dir)
        if load_env:
            load_dotenv()
        main = _read_toml(cdir / "config.toml")
        strat = _read_toml(cdir / "strategy.toml")
        mkts = _read_toml(cdir / "markets.toml")

        profiles = {
            name: StrategyProfile(**params)
            for name, params in (strat.get("profiles") or {}).items()
        }
        markets = [MarketEntry(**m) for m in (mkts.get("markets") or [])]

        risk = RiskConfig(**main.get("risk", {})).resolve_from_bankroll()
        return cls(
            wallet=WalletConfig(**main.get("wallet", {})),
            engine=EngineConfig(**main.get("engine", {})),
            risk=risk,
            execution=ExecutionConfig(**main.get("execution", {})),
            paths=PathsConfig(**main.get("paths", {})),
            profiles=profiles,
            markets=markets,
            secrets=Secrets(),
            config_dir=cdir,
        )

    


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)
