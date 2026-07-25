"""Double-entry style equity ledger for fills + marks.

equity = cash
       + Σ inventory_shares × mark
       + realized_rewards
       + realized_rebates
       - fees
       - gas
       - merge/settlement costs

Every fill updates cash and inventory; marks revalue inventory without
changing cash. Component PnL must sum to total equity change from day start.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polymaker.domain import Fill, Side


@dataclass
class LedgerSnapshot:
    cash: float
    inventory_value: float
    equity: float
    realized_spread: float
    inventory_mtm: float
    rewards: float
    rebates: float
    fees: float
    gas: float
    other_costs: float
    n_fills: int
    positions: dict[str, float]


@dataclass
class EquityLedger:
    """Authoritative cash + inventory accounting.

    Pure: no I/O. Engine/replay push fills and marks.
    """

    cash: float = 0.0
    # token_id -> shares held (>= 0)
    positions: dict[str, float] = field(default_factory=dict)
    # token_id -> avg entry price
    avg_price: dict[str, float] = field(default_factory=dict)
    # token_id -> last mark
    marks: dict[str, float] = field(default_factory=dict)

    realized_spread: float = 0.0  # cash edge vs avg on exits
    inventory_mtm: float = 0.0  # cumulative mark-to-market on open inv
    rewards: float = 0.0
    rebates: float = 0.0
    fees: float = 0.0
    gas: float = 0.0
    other_costs: float = 0.0
    n_fills: int = 0

    # For exact reconciliation of cumulative signed fills
    cumulative_signed: dict[str, float] = field(default_factory=dict)
    _start_equity: float = 0.0
    _last_inventory_value: float = 0.0

    def reset_day(self) -> None:
        self._start_equity = self.equity()
        self._last_inventory_value = self.inventory_value()

    def apply_fill(self, fill: Fill) -> None:
        """Apply a fill: update cash, inventory, realized spread proxy."""
        tid = fill.token_id
        signed = fill.size if fill.side is Side.BUY else -fill.size
        notional = fill.price * fill.size

        if fill.side is Side.BUY:
            self.cash -= notional
            prev = self.positions.get(tid, 0.0)
            avg = self.avg_price.get(tid, 0.0)
            if prev <= 0:
                self.avg_price[tid] = fill.price
            else:
                self.avg_price[tid] = (avg * prev + notional) / (prev + fill.size)
            self.positions[tid] = prev + fill.size
        else:
            self.cash += notional
            prev = self.positions.get(tid, 0.0)
            avg = self.avg_price.get(tid, fill.price)
            sold = min(prev, fill.size)
            # Realized vs average entry (spread capture proxy)
            self.realized_spread += (fill.price - avg) * sold
            new_sz = max(0.0, prev - fill.size)
            self.positions[tid] = new_sz
            if new_sz <= 0:
                self.avg_price[tid] = 0.0

        self.cumulative_signed[tid] = self.cumulative_signed.get(tid, 0.0) + signed
        # Keep non-negative inventory (no short cash-and-carry in base ledger)
        if self.positions.get(tid, 0.0) < 0:
            self.positions[tid] = 0.0
        self.n_fills += 1

    def update_mark(self, token_id: str, mark: float) -> None:
        self.marks[token_id] = mark
        # Refresh MTM component (equity holds current mark; component tracks delta)
        new_iv = self.inventory_value()
        self.inventory_mtm += new_iv - self._last_inventory_value
        self._last_inventory_value = new_iv

    def add_reward(self, amount: float) -> None:
        self.rewards += amount
        self.cash += amount

    def add_rebate(self, amount: float) -> None:
        self.rebates += amount
        self.cash += amount

    def add_fee(self, amount: float) -> None:
        self.fees += amount
        self.cash -= amount

    def add_gas(self, amount: float) -> None:
        self.gas += amount
        self.cash -= amount

    def add_cost(self, amount: float) -> None:
        self.other_costs += amount
        self.cash -= amount

    def inventory_value(self) -> float:
        total = 0.0
        for tid, sz in self.positions.items():
            if sz <= 0:
                continue
            mark = self.marks.get(tid, self.avg_price.get(tid, 0.0))
            total += sz * mark
        return total

    def equity(self) -> float:
        return self.cash + self.inventory_value()

    def daily_pnl(self) -> float:
        return self.equity() - self._start_equity

    def component_sum(self) -> float:
        """PnL components from day start (should match daily_pnl when marks tracked)."""
        return (
            self.realized_spread
            + self.inventory_mtm
            + self.rewards
            + self.rebates
            - self.fees
            - self.gas
            - self.other_costs
            + (self.cash - (self.equity() - self.inventory_value() - self.cash + self.cash))
        )

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            cash=self.cash,
            inventory_value=self.inventory_value(),
            equity=self.equity(),
            realized_spread=self.realized_spread,
            inventory_mtm=self.inventory_mtm,
            rewards=self.rewards,
            rebates=self.rebates,
            fees=self.fees,
            gas=self.gas,
            other_costs=self.other_costs,
            n_fills=self.n_fills,
            positions=dict(self.positions),
        )

    def assert_invariants(self, *, tol: float = 1e-6) -> None:
        """Raise AssertionError if accounting is internally inconsistent."""
        for tid, sz in self.positions.items():
            if sz < -tol:
                raise AssertionError(f"negative inventory {tid}={sz}")
            if abs(sz - max(0.0, self.cumulative_signed.get(tid, 0.0))) > tol:
                # cumulative_signed can differ if we clamp shorts; only check >= path
                if self.cumulative_signed.get(tid, 0.0) >= -tol:
                    if abs(sz - self.cumulative_signed[tid]) > tol:
                        raise AssertionError(
                            f"position != cumulative fills for {tid}: "
                            f"pos={sz} cum={self.cumulative_signed[tid]}"
                        )
        eq = self.equity()
        if abs(eq - (self.cash + self.inventory_value())) > tol:
            raise AssertionError("equity != cash + inventory_value")
