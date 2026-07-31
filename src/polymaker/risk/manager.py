"""RiskManager: pre-trade gates and circuit breakers (see the README).

Consulted by the engine before every quote set. Returns a per-market decision
(size scale / reduce-only / halt) and owns the global kill switches. Position
and order data come from the StateStore; fair-value marks are pushed in by the
engine so PnL is always current.
"""

from __future__ import annotations

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
        self._net_cash = 0.0  # cumulative signed cash from fills (+sell, -buy)
        self._day_start_equity = 0.0
        self._killed = False
        self._order_attempts = 0
        self._order_errors = 0
        self._cumulative_gas_cost = 0.0  # cumulative on-chain gas cost (USDC)
        self._per_token_realized_pnl: dict[str, float] = {}  # token_id -> cumulative realized PnL from fills

    @property
    def cfg(self) -> RiskConfig:
        """Resolved risk config (absolute caps derived from bankroll when set)."""
        return self._cfg

    # ── PnL bookkeeping ─────────────────────────────────────────────────
    def note_fill(self, fill: Fill) -> None:
        self._net_cash += (fill.price * fill.size) * (1 if fill.side is Side.SELL else -1)
        if fill.side is Side.SELL:
            pos = self._store.position(fill.token_id)
            cost_basis = pos.avg_price if pos.avg_price > 0 else fill.price
            realized = (fill.price - cost_basis) * fill.size
            self._per_token_realized_pnl[fill.token_id] = (
                self._per_token_realized_pnl.get(fill.token_id, 0.0) + realized
            )

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
        self._day_start_equity = self.equity

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
        if self._cfg.daily_loss_kill_usdc > 0 and self.daily_pnl <= -self._cfg.daily_loss_kill_usdc:
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
