"""Portfolio-level intelligence: capital allocation, correlation, diversification.

A single market is simple: quote, fill, earn. Multiple markets are
complex: how much capital to allocate to each, how to diversify,
when to reduce exposure.

This module answers:
1. How much capital should each market get?
2. How correlated are my markets? (diversification)
3. What's my total exposure? (risk)
4. Should I rebalance? (when to move capital)

Approach: Mean-variance optimization with constraints.
For each market, we have:
- Expected return (from intelligence features)
- Risk (volatility)
- Correlation with other markets (estimated from price co-movement)

The allocator computes:
- Optimal weights using mean-variance optimization
- Maximum concentration per market (to avoid single-market risk)
- Total capital constraint

This is a classical Markowitz-style portfolio optimization, adapted
for a market-making context where "return" is the expected reward
accrual and "risk" is the variance of fill outcomes.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from polymaker.strategy.allocation import (
    AllocationInputs,
    MarketAllocation,
    allocate_capital,
)
from polymaker.intelligence.decision import (
    DecisionFramework,
    TradingDecision,
)


@dataclass
class MarketAllocationState:
    """Per-market state for portfolio allocation.

    Tracks expected return, risk, and current allocation.
    """

    condition_id: str
    expected_return: float = 0.0  # $/day
    risk: float = 0.01  # per-day std dev
    current_allocation: float = 0.0  # current capital allocated
    target_allocation: float = 0.0  # target from optimizer
    correlation: dict[str, float] = field(default_factory=dict)
    decision: TradingDecision | None = None


@dataclass
class PortfolioState:
    """Portfolio-level state for capital allocation.

    Maintains per-market allocation states and computes the optimal
    capital distribution using mean-variance optimization.
    """

    markets: dict[str, MarketAllocationState] = field(default_factory=dict)
    total_capital: float = 100.0
    max_concentration: float = 0.5
    last_rebalance_ts: float = 0.0
    rebalance_count: int = 0

    def update_market(
        self,
        condition_id: str,
        expected_return: float,
        risk: float,
        decision: TradingDecision | None = None,
    ) -> None:
        """Update a market's expected return and risk."""
        if condition_id not in self.markets:
            self.markets[condition_id] = MarketAllocationState(
                condition_id=condition_id
            )
        self.markets[condition_id].expected_return = expected_return
        self.markets[condition_id].risk = risk
        if decision is not None:
            self.markets[condition_id].decision = decision

    def update_correlation(
        self, cid_a: str, cid_b: str, correlation: float
    ) -> None:
        """Update correlation between two markets."""
        if cid_a not in self.markets:
            self.markets[cid_a] = MarketAllocationState(
                condition_id=cid_a
            )
        if cid_b not in self.markets:
            self.markets[cid_b] = MarketAllocationState(
                condition_id=cid_b
            )
        self.markets[cid_a].correlation[cid_b] = correlation
        self.markets[cid_b].correlation[cid_a] = correlation

    def compute_target_allocations(self) -> dict[str, float]:
        """Compute optimal capital allocation across markets.

        Uses mean-variance optimization with max concentration cap.
        For simplicity, we use a risk-parity approach:
          weight_i = expected_return_i / risk_i^2

        Then normalize and apply max concentration cap.
        """
        if not self.markets:
            return {}
        # Only include markets that should_quote
        eligible = {
            cid: m for cid, m in self.markets.items()
            if m.decision is None or m.decision.should_quote
        }
        if not eligible:
            return {}
        # Build allocation inputs
        alloc_rows = []
        for cid, m in eligible.items():
            if m.expected_return <= 0 or m.risk <= 0:
                continue
            # Expected return / risk^2
            alloc_rows.append((cid, m.expected_return, m.risk))
        if not alloc_rows:
            return {}
        result = allocate_capital(AllocationInputs(
            markets=tuple(alloc_rows),
            total_capital_usdc=self.total_capital,
            max_concentration=self.max_concentration,
            min_allocation=0.05,
        ))
        targets = {}
        for a in result.allocations:
            targets[a.condition_id] = a.capital_usdc
        return targets

    def rebalance(
        self, ts: float, total_capital: float | None = None,
        max_concentration: float | None = None,
    ) -> dict[str, float]:
        """Rebalance capital across markets.

        Returns dict of {cid: new_allocation}.
        Updates each market's target_allocation.
        """
        if total_capital is not None:
            self.total_capital = total_capital
        if max_concentration is not None:
            self.max_concentration = max_concentration
        targets = self.compute_target_allocations()
        for cid, target in targets.items():
            if cid in self.markets:
                self.markets[cid].target_allocation = target
        self.last_rebalance_ts = ts
        self.rebalance_count += 1
        return targets

    def total_exposure(self) -> float:
        """Sum of current allocations across all markets."""
        return sum(
            m.current_allocation for m in self.markets.values()
        )

    def expected_portfolio_return(self) -> float:
        """Sum of expected return weighted by current allocation."""
        total = 0.0
        for m in self.markets.values():
            total += m.current_allocation * m.expected_return
        return total

    def portfolio_risk(self, n_days: float = 1.0) -> float:
        """Portfolio variance (sqrt) over n_days.

        Risk = sqrt(sum_i sum_j w_i * w_j * cov_ij)
        where cov_ij = risk_i * risk_j * corr_ij
        For i == j, corr_ii = 1.
        """
        if not self.markets:
            return 0.0
        markets_list = list(self.markets.values())
        var = 0.0
        for i, mi in enumerate(markets_list):
            wi = mi.current_allocation
            for j, mj in enumerate(markets_list):
                wj = mj.current_allocation
                corr = 1.0 if i == j else mi.correlation.get(
                    mj.condition_id, 0.0
                )
                var += wi * wj * mi.risk * mj.risk * corr
        return math.sqrt(max(0, var)) * math.sqrt(n_days)

    def sharpe_ratio(self, n_days: float = 1.0) -> float:
        """Portfolio Sharpe ratio = return / risk."""
        risk = self.portfolio_risk(n_days)
        if risk < 1e-9:
            return 0.0
        return self.expected_portfolio_return() / risk
