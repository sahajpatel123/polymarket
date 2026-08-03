"""RiskManager: pre-trade gates and circuit breakers (see the README).

Consulted by the engine before every quote set. Returns a per-market decision
(size scale / reduce-only / halt) and owns the global kill switches. Position
and order data come from the StateStore; fair-value marks are pushed in by the
engine so PnL is always current.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from polymaker.config import RiskConfig
from polymaker.domain import Fill, MarketMeta, Side
from polymaker.logging import get_logger
from polymaker.state.store import StateStore

log = get_logger("risk.manager")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    halt: bool  # HALTED regime for this market
    reduce_only: bool  # REDUCE_ONLY regime for this market
    size_scale: float  # multiply quote sizes by this [0,1]
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, store: StateStore) -> None:
        # Always store bankroll-resolved caps so absolute limits match policy.
        self._cfg = cfg.resolve_from_bankroll()
        self._store = store
        self._marks: dict[str, float] = {}  # token_id -> fair value
        # Seed from persisted fills: this counter is in-memory, so a restart
        # otherwise zeroed the cash side while positions reloaded in full,
        # overstating equity by everything spent before the restart and
        # suppressing the daily-loss stop.
        try:
            self._net_cash = float(store.net_cash_from_fills())
        except Exception:
            log.warning("net_cash_seed_failed", exc_info=True)
            self._net_cash = 0.0
        self._day_start_equity = 0.0
        self._day_anchor_day = ""
        self._killed = False
        # Latched daily-loss breach. The daily cap is a STOP for the day, not a
        # throttle: once breached it must stay engaged even if mark-to-market
        # bounces back above the threshold. Without the latch the halt released
        # on every favourable tick and the engine resumed adding exposure, so a
        # $10 cap walked a $100 book down past -$600 in one session.
        # Cleared only by reset_day().
        self._daily_loss_latched = False
        self._order_attempts = 0
        self._order_errors = 0
        self._cumulative_gas_cost = 0.0  # cumulative on-chain gas cost (USDC)
        self._per_token_realized_pnl: dict[str, float] = {}  # token_id -> cumulative realized PnL from fills

    @property
    def cfg(self) -> RiskConfig:
        """Resolved risk config (absolute caps derived from bankroll when set)."""
        return self._cfg

    # ── PnL bookkeeping ─────────────────────────────────────────────────
    def note_fill(self, fill: Fill, *, cost_basis: float | None = None) -> float | None:
        """Book a fill. Returns realized PnL when this SELL closes inventory.

        The return value is the only *money* outcome the engine can observe per
        trade, so the win-rate governor is driven from it rather than from a
        30-second fair-value markout (which mislabels spread capture: buying the
        bid and selling the ask is profitable even when fair value never moved).

        ``cost_basis`` is the pre-fill average price captured by the caller
        BEFORE the state store applies the fill. ``apply_fill`` zeroes
        ``avg_price`` when a SELL closes the position, so reading the position
        inside this method would always see a 0 cost basis for full round trips
        and the realized PnL (and the governor's win/loss) would never fire.
        """
        self._net_cash += (fill.price * fill.size) * (1 if fill.side is Side.SELL else -1)
        if fill.side is not Side.SELL:
            return None
        pos = self._store.position(fill.token_id)
        basis = cost_basis if cost_basis is not None and cost_basis > 0.0 else pos.avg_price
        if basis <= 0.0:
            # No cost basis: this is a short, not a closed round trip. Scoring it
            # would feed the governor a 0-PnL "loss" and throttle entries on a
            # trade that never had an outcome.
            return None
        realized = (fill.price - basis) * fill.size
        self._per_token_realized_pnl[fill.token_id] = (
            self._per_token_realized_pnl.get(fill.token_id, 0.0) + realized
        )
        return realized

    def update_mark(self, token_id: str, fv: float) -> None:
        self._marks[token_id] = fv

    def _inventory_value(self) -> float:
        total = 0.0
        for tok, pos in self._store.positions.items():
            if pos.size > 0:
                total += pos.size * self._marks.get(tok, pos.avg_price)
        return total

    @property
    def net_cash(self) -> float:
        return self._net_cash

    @property
    def inventory_value(self) -> float:
        return self._inventory_value()

    @property
    def equity(self) -> float:
        return self._net_cash + self._inventory_value()

    @property
    def daily_pnl(self) -> float:
        return self.equity - self._day_start_equity

    @property
    def day_start_equity(self) -> float:
        """Equity snapshot at last reset_day() — used by degradation detector."""
        return self._day_start_equity

    def reset_day(self) -> None:
        """Rebase the daily loss budget to current equity (explicit new day)."""
        self._day_start_equity = self.equity
        # A new day releases the daily-loss stop.
        self._daily_loss_latched = False
        self._day_anchor_day = self._today()
        self._persist_day_anchor()

    def begin_day(self) -> None:
        """Startup path: resume today's budget if one already exists.

        ``reset_day()`` on every process start let a restart launder the day's
        loss — an engine already down $64 came back with a fresh $10 allowance,
        so the daily cap never actually stopped anything across restarts. This
        restores the persisted anchor (and latch) when it is still the same UTC
        day, and only rebases on a genuinely new day.
        """
        day = self._today()
        self._day_anchor_day = day
        existing = self._store.get_day_anchor(day)
        if existing is None:
            self.reset_day()
            return
        self._day_start_equity, self._daily_loss_latched = existing

    def rollover_if_new_day(self) -> None:
        """Mid-run UTC rollover: rebase the daily-loss budget on a new day.

        ``begin_day`` only runs at startup; a process that crosses UTC
        midnight would otherwise keep yesterday's equity snapshot and an
        already-latched loss until restart. This mirrors ``begin_day``'s
        anchor semantics on every maintenance tick.
        """
        day = self._today()
        if day != self._day_anchor_day:
            self._day_anchor_day = day
            self.reset_day()

    @staticmethod
    def _today() -> str:
        return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")

    def _persist_day_anchor(self) -> None:
        try:
            self._store.set_day_anchor(
                self._today(), self._day_start_equity, self._daily_loss_latched
            )
        except Exception:  # pragma: no cover - persistence must never block risk
            log.warning("day_anchor_persist_failed", exc_info=True)

    # ── error-rate breaker ──────────────────────────────────────────────
    def note_order_result(self, ok: bool, reason: str = "") -> None:
        """Record an order placement result.

        ok=True increments attempts only. ok=False increments both
        attempts and errors (used for the max_order_error_rate breaker).
        reason is logged for diagnostics but does not affect the breaker.
        """
        self._order_attempts += 1
        if not ok:
            self._order_errors += 1
            log.warning("order_failed", reason=reason)

    @property
    def error_rate(self) -> float:
        return self._order_errors / self._order_attempts if self._order_attempts >= 20 else 0.0

    # ── global kill switch ──────────────────────────────────────────────
    def global_halt(self) -> tuple[bool, str]:
        if self._killed:
            return True, "manual_kill"
        if (
            self._cfg.daily_loss_kill_usdc > 0
            and self.daily_pnl <= -self._cfg.daily_loss_kill_usdc
            and not self._daily_loss_latched
        ):
            # Latch once, on the transition — persisting every cycle would
            # hammer SQLite (the halt is evaluated on every quote cycle).
            self._daily_loss_latched = True
            self._persist_day_anchor()
        if self._daily_loss_latched:
            return True, f"daily_loss {self.daily_pnl:.0f}"
        if self.error_rate >= self._cfg.max_order_error_rate:
            return True, f"error_rate {self.error_rate:.2f}"
        # Gas cost circuit breaker: if cumulative on-chain gas costs exceed
        # the threshold fraction of starting equity, kill. On Polygon,
        # a single merge tx can cost $1-5; with $30 capital, one bad merge
        # = 3-17% gone. This is the circuit breaker that prevents that.
        # Use max_total_exposure as a fallback reference when day_start_equity
        # is 0 (e.g., on a fresh account with no inventory yet).
        gas_ref = self._day_start_equity if self._day_start_equity > 0 else self._cfg.max_total_exposure_usdc
        if gas_ref > 0:
            gas_frac = self._cumulative_gas_cost / gas_ref
            if gas_frac >= self._cfg.max_gas_cost_pct:
                return True, f"gas_cost {gas_frac:.1%}>={self._cfg.max_gas_cost_pct:.0%}"
        return False, ""

    def note_gas_cost(self, cost_usdc: float) -> None:
        """Record an on-chain gas cost. Triggers circuit breaker if cumulative
        costs exceed max_gas_cost_pct of starting equity.
        """
        if cost_usdc <= 0:
            return
        self._cumulative_gas_cost += cost_usdc
        log.info("gas_cost_recorded", cost=cost_usdc,
                 cumulative=round(self._cumulative_gas_cost, 4))

    @property
    def cumulative_gas_cost(self) -> float:
        return self._cumulative_gas_cost

    def kill(self) -> None:
        self._killed = True
        log.critical("kill_switch_engaged")

    # ── per-market evaluation ───────────────────────────────────────────
    def evaluate(
        self, meta: MarketMeta, *, ws_stale: bool, event_group_cost: float
    ) -> RiskDecision:
        halted, why = self.global_halt()
        if halted:
            return RiskDecision(True, False, 0.0, why)
        if ws_stale:
            return RiskDecision(True, False, 0.0, "ws_stale")

        market_notional = self._market_notional(meta)
        total_exposure = self._total_exposure()

        # hard caps -> reduce only
        if market_notional >= self._cfg.max_market_notional_usdc:
            return RiskDecision(False, True, 1.0, "market_cap")
        if event_group_cost >= self._cfg.max_event_group_loss_usdc:
            return RiskDecision(False, True, 1.0, "event_group_cap")
        if total_exposure >= self._cfg.max_total_exposure_usdc:
            return RiskDecision(False, True, 1.0, "total_exposure_cap")

        # Per-market concentration: don't allow > 50% of capital in one market.
        # Without this, $30 in one toxic market = total wipeout. Fixed from
        # the old hardcoded $400 cap which was meaningless for small accounts.
        # Only triggers if the cap is below the configured hard market cap
        # (otherwise the hard cap is the binding constraint).
        concentration_cap = min(
            self._cfg.max_market_notional_usdc,
            self._cfg.max_total_exposure_usdc * self._cfg.max_market_concentration_pct,
        )
        if concentration_cap > 0 and market_notional > concentration_cap:
            return RiskDecision(
                False, True, 1.0,
                f"market_concentration>{concentration_cap:.0f}"
            )

        # Per-market PnL kill-switch: if a market has lost more than the
        # threshold, halt quoting on it (reduce-only, no new entries).
        market_pnl = self._market_pnl(meta)
        if market_pnl <= -self._cfg.max_market_loss_usdc:
            return RiskDecision(False, True, 1.0, f"market_loss>{self._cfg.max_market_loss_usdc:.0f}")

        # soft scaling: taper size as any cap is approached (worst-binding wins)
        scale = min(
            _headroom(market_notional, self._cfg.max_market_notional_usdc),
            _headroom(total_exposure, self._cfg.max_total_exposure_usdc),
            _headroom(event_group_cost, self._cfg.max_event_group_loss_usdc),
        )
        return RiskDecision(False, False, scale, "")

    def _market_notional(self, meta: MarketMeta) -> float:
        """Filled-inventory notional for this market. Deliberately does NOT count
        our own resting BUY orders: those are the quotes we're about to replace,
        and counting them makes the size taper collapse the moment we place a full
        quote (self-reinforcing cancel/replace churn). Worst-case fill is bounded
        instead by small per-quote sizes + the position cap that this drives."""
        total = 0.0
        for tok in (meta.yes.token_id, meta.no.token_id):
            pos = self._store.position(tok)
            total += pos.size * self._marks.get(tok, pos.avg_price or 0.5)
        return total

    def _total_exposure(self) -> float:
        total = 0.0
        for tok, pos in self._store.positions.items():
            if pos.size > 0:
                total += pos.size * self._marks.get(tok, pos.avg_price or 0.5)
        return total

    def _market_pnl(self, meta: MarketMeta) -> float:
        """Per-market realized + unrealized PnL for kill-switch monitoring.

        Realized: cumulative PnL from completed SELL fills on this market's tokens.
        Unrealized: mark-to-market drift on remaining inventory vs avg_price.
        """
        realized = 0.0
        for tok in (meta.yes.token_id, meta.no.token_id):
            realized += self._per_token_realized_pnl.get(tok, 0.0)
        unrealized = 0.0
        for tok in (meta.yes.token_id, meta.no.token_id):
            pos = self._store.position(tok)
            if pos.size <= 0:
                continue
            mark = self._marks.get(tok, pos.avg_price or 0.5)
            unrealized += (mark - pos.avg_price) * pos.size
        return realized + unrealized


def _headroom(current: float, cap: float) -> float:
    """1.0 well below the cap, tapering to 0 as we approach it (from 70%)."""
    if cap <= 0:
        return 1.0
    frac = current / cap
    if frac <= 0.7:
        return 1.0
    return max(0.0, (1.0 - frac) / 0.3)
