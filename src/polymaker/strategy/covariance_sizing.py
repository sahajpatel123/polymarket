"""Multi-Market Covariance Position Sizing.

When market-making correlated markets simultaneously (e.g. multiple candidate
outcomes in a presidential primary or related neg-risk event groups), independent
position sizing ignores cross-market correlation and risks over-allocating capital.

Formula:
  Let q be the vector of proposed positions (shares or notional in USDC).
  Let Sigma be the cross-market return/price variance-covariance matrix.
  The total portfolio variance is: Var(P) = q^T * Sigma * q.

  If Var(P) > MaxPortfolioVariance:
      scaling_factor = sqrt(MaxPortfolioVariance / Var(P))
      q_adjusted = scaling_factor * q

Pure functions only — no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class MultiMarketSizingResult:
    original_notionals: tuple[float, ...]
    adjusted_notionals: tuple[float, ...]
    portfolio_variance: float
    max_allowed_variance: float
    scaling_factor: float


def compute_covariance_matrix(
    returns_by_market: Sequence[Sequence[float]]
) -> list[list[float]]:
    """Compute N x N sample covariance matrix from asset return series.
    
    returns_by_market: list of length N, each element a sequence of return observations.
    """
    n_markets = len(returns_by_market)
    if n_markets == 0:
        return []

    n_samples = min(len(r) for r in returns_by_market)
    if n_samples < 2:
        # Fallback to identity matrix if insufficient samples
        return [[1.0 if i == j else 0.0 for j in range(n_markets)] for i in range(n_markets)]

    means = [sum(r[:n_samples]) / n_samples for r in returns_by_market]

    cov = [[0.0] * n_markets for _ in range(n_markets)]
    for i in range(n_markets):
        for j in range(i, n_markets):
            s = sum(
                (returns_by_market[i][k] - means[i]) * (returns_by_market[j][k] - means[j])
                for k in range(n_samples)
            )
            val = s / (n_samples - 1)
            cov[i][j] = val
            cov[j][i] = val

    return cov


def scale_correlated_positions(
    proposed_notionals: Sequence[float],
    cov_matrix: Sequence[Sequence[float]],
    max_portfolio_variance: float,
) -> MultiMarketSizingResult:
    """Scale proposed positions so that total portfolio variance does not exceed max_portfolio_variance.
    
    Var(P) = q^T * Cov * q
    """
    n = len(proposed_notionals)
    if n == 0 or not cov_matrix or len(cov_matrix) != n or max_portfolio_variance <= 0:
        return MultiMarketSizingResult(
            original_notionals=tuple(proposed_notionals),
            adjusted_notionals=tuple(proposed_notionals),
            portfolio_variance=0.0,
            max_allowed_variance=max_portfolio_variance,
            scaling_factor=1.0,
        )

    # Compute q^T * Cov * q
    port_var = 0.0
    for i in range(n):
        for j in range(n):
            port_var += proposed_notionals[i] * cov_matrix[i][j] * proposed_notionals[j]

    port_var = max(0.0, port_var)

    if port_var > max_portfolio_variance and port_var > 1e-12:
        scale = math.sqrt(max_portfolio_variance / port_var)
    else:
        scale = 1.0

    adjusted = tuple(round(q * scale, 4) for q in proposed_notionals)
    adjusted_var = port_var * (scale * scale)

    return MultiMarketSizingResult(
        original_notionals=tuple(proposed_notionals),
        adjusted_notionals=adjusted,
        portfolio_variance=round(adjusted_var, 6),
        max_allowed_variance=round(max_portfolio_variance, 6),
        scaling_factor=round(scale, 6),
    )

