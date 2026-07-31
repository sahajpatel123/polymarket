"""Online parameter optimization via CMA-ES per market.

Pillar 3 of the S-tier architecture: every fill is a data point. Instead of
static TOML knobs, a per-market CMA-ES optimiser hill-climbs on the edge/sharpe
from recent fills. When the regime shifts, the optimiser catches it before any
hand-tuned rule does.

CMA-ES (Covariance Matrix Adaptation Evolution Strategy):
  Maintains a multivariate normal (mean, step-size, covariance) over the
  parameter space. Each generation samples λ candidates, evaluates them on
  recent fill data, and updates the distribution to move toward better parameters.

The optimiser replaces static values for these knobs:
  - delta_min_ticks: minimum half-spread in ticks
  - layer_step_ticks: price step between layers
  - flow_fv_weight: how much flow_z nudges FV
  - gamma: inventory skew coefficient
  - c_vol: vol contribution to half-spread
  - c_tox: toxicity contribution to half-spread
  - base_size_mult: multiplier on base_size_usdc
  - spread_mult: multiplier on half-spread
  - kelly_fraction: fraction of Kelly for sizing asharing
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class OptimizerParams:
    """Parameters under optimization, with ranges and scaling."""

    delta_min_ticks: float = 1.0  # [0.5, 8.0]
    layer_step_ticks: float = 2.0  # [1.0, 10.0]
    flow_fv_weight: float = 0.5  # [0.0, 1.0]
    gamma: float = 0.1  # [0.0, 0.5]
    c_vol: float = 2.0  # [0.5, 8.0]
    c_tox: float = 15.0  # [2.0, 40.0]
    base_size_mult: float = 1.0  # [0.2, 3.0]
    spread_mult: float = 1.0  # [0.5, 3.0]
    kelly_fraction: float = 0.25  # [0.05, 1.0]

    _param_names: tuple = field(
        default=(
            "delta_min_ticks", "layer_step_ticks", "flow_fv_weight",
            "gamma", "c_vol", "c_tox", "base_size_mult",
            "spread_mult", "kelly_fraction",
        ),
        init=False,
        repr=False,
    )

    _param_bounds: tuple = field(
        default=(
            (0.5, 8.0), (1.0, 10.0), (0.0, 1.0),
            (0.0, 0.5), (0.5, 8.0), (2.0, 40.0),
            (0.2, 3.0), (0.5, 3.0), (0.05, 1.0),
        ),
        init=False,
        repr=False,
    )

    def to_array(self) -> np.ndarray:
        return np.array([getattr(self, n) for n in self._param_names], dtype=np.float64)

    @classmethod
    def from_array(cls, arr: np.ndarray) -> OptimizerParams:
        return cls(**{n: float(arr[i]) for i, n in enumerate(cls._param_names)})

    def clamp(self) -> OptimizerParams:
        for i, name in enumerate(self._param_names):
            lo, hi = self._param_bounds[i]
            setattr(self, name, max(lo, min(hi, getattr(self, name))))
        return self

    def to_profile_overrides(self) -> dict[str, float]:
        return {
            "delta_min_ticks": int(round(self.delta_min_ticks)),
            "layer_step_ticks": int(round(self.layer_step_ticks)),
            "flow_fv_weight": round(self.flow_fv_weight, 3),
            "gamma": round(self.gamma, 3),
            "c_vol": round(self.c_vol, 3),
            "c_tox": round(self.c_tox, 3),
        }


# ── fill outcome (one data point) ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FillOutcome:
    params: OptimizerParams
    markout: float  # signed, positive = price moved in our favour after fill
    fill_size: float  # shares filled
    fill_price: float
    ts: float

    @property
    def edge(self) -> float:
        return self.markout * self.fill_size


# ── CMA-ES implementation ──────────────────────────────────────────────────


class CMAESOptimizer:
    """Per-market CMA-ES optimiser that hill-climbs on edge/sharpe.

    λ = 4 + ⌊3·log(D)⌋ samples per generation (D = parameter dimension).
    μ = ⌊λ/2⌋ elite samples drive the mean update.
    """

    def __init__(
        self,
        *,
        initial: OptimizerParams | None = None,
        sigma: float = 0.3,
        max_generations: int = 50,
        patience: int = 10,
        min_improvement: float = 0.001,
    ) -> None:
        self._dim = 9
        self._lambda = 4 + int(3.0 * math.log(self._dim))
        self._mu = self._lambda // 2
        self._sigma = sigma
        self._max_gens = max_generations
        self._patience = patience
        self._min_improvement = min_improvement

        self._mean = (initial or OptimizerParams()).to_array()
        self._cov = np.eye(self._dim) * sigma * sigma
        self._best_params = self._mean.copy()
        self._best_fitness = -np.inf
        self._generation = 0
        self._no_improvement = 0

        self._weights = np.log(self._mu + 0.5) - np.log(np.arange(1, self._mu + 1))
        self._weights /= self._weights.sum()
        self._mu_eff = 1.0 / (self._weights**2).sum()

        self._cs = 0.0
        self._ds = 1.0
        self._cc = (4.0 + self._mu_eff / self._dim) / (self._dim + 4.0 + 2.0 * self._mu_eff / self._dim)
        self._c1 = 2.0 / ((self._dim + 1.3) ** 2 + self._mu_eff)
        self._cmu = min(1.0 - self._c1, 2.0 * (self._mu_eff - 2.0 + 1.0 / self._mu_eff) /
                        ((self._dim + 2.0) ** 2 + self._mu_eff))
        self._chi = math.sqrt(self._dim) * (1.0 - 1.0 / (4.0 * self._dim) + 1.0 / (21.0 * self._dim**2))
        self._pc = np.zeros(self._dim)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def best_fitness(self) -> float:
        return float(self._best_fitness)

    def best_params(self) -> OptimizerParams:
        return OptimizerParams.from_array(self._best_params).clamp()

    def ask(self) -> list[np.ndarray]:
        candidates = np.random.multivariate_normal(self._mean, self._cov, size=self._lambda)
        for i in range(self._lambda):
            for j in range(self._dim):
                lo, hi = OptimizerParams._param_bounds[j]
                candidates[i, j] = max(lo, min(hi, candidates[i, j]))
        return [candidates[i] for i in range(self._lambda)]

    def tell(self, solutions: list[np.ndarray], fitnesses: list[float]) -> bool:
        """Update distribution from evaluated solutions. Returns True if improved."""
        if len(solutions) != self._lambda or len(fitnesses) != self._lambda:
            return False

        order = np.argsort(fitnesses)[::-1]
        elites = [solutions[i] for i in order[:self._mu]]
        elite_f = [fitnesses[i] for i in order[:self._mu]]

        old_mean = self._mean.copy()
        self._mean = sum(w * e for w, e in zip(self._weights, elites))

        y = (self._mean - old_mean) / self._sigma
        self._ps = (1.0 - self._cs) * (getattr(self, "_ps", np.zeros(self._dim))) + \
                   math.sqrt(self._cs * (2.0 - self._cs) * self._mu_eff) * \
                   np.linalg.solve(np.linalg.cholesky(self._cov).T, y)
        hsig = np.linalg.norm(getattr(self, "_ps", np.zeros(self._dim))) / \
               math.sqrt(1.0 - (1.0 - self._cs) ** (2 * (self._generation + 1))) < \
               (1.4 + 2.0 / (self._dim + 1.0)) * self._chi
        self._pc = (1.0 - self._cc) * self._pc + hsig * \
                   math.sqrt(self._cc * (2.0 - self._cc) * self._mu_eff) * y

        artmp = np.array([(e - old_mean) / self._sigma for e in elites])
        self._cov = (1.0 - self._c1 - self._cmu) * self._cov + \
                    self._c1 * (np.outer(self._pc, self._pc) + (1.0 - hsig) * self._cc * (2.0 - self._cc) * self._cov) + \
                    self._cmu * sum(w * np.outer(a, a) for w, a in zip(self._weights, artmp))

        self._sigma *= math.exp(
            (self._cs / self._ds) * (np.linalg.norm(getattr(self, "_ps", np.zeros(self._dim))) / self._chi - 1.0)
        )
        self._sigma = max(self._sigma, 1e-10)

        self._generation += 1

        best_f = elite_f[0]
        if best_f > self._best_fitness + self._min_improvement:
            self._best_fitness = best_f
            self._best_params = elites[0].copy()
            self._no_improvement = 0
            return True
        self._no_improvement += 1
        return False

    def should_stop(self) -> bool:
        if self._no_improvement >= self._patience:
            return True
        if self._generation >= self._max_gens:
            return True
        if self._sigma < 1e-8:
            return True
        return False

    def reset(self, initial: OptimizerParams | None = None) -> None:
        self._mean = (initial or OptimizerParams()).to_array()
        self._cov = np.eye(self._dim) * 0.3 * 0.3
        self._best_params = self._mean.copy()
        self._best_fitness = -np.inf
        self._generation = 0
        self._no_improvement = 0
        self._pc = np.zeros(self._dim)


# ── per-market online optimiser ────────────────────────────────────────────


@dataclass
class MarketOptimizer:
    """Wraps CMA-ES for one market with fill data accumulation."""

    condition_id: str
    optimizer: CMAESOptimizer = field(default_factory=CMAESOptimizer)
    outcomes: list[FillOutcome] = field(default_factory=list)
    max_outcomes: int = 5000  # rolling window for recent fills
    retrain_every: int = 50  # run one generation every N fills
    last_params: OptimizerParams = field(default_factory=OptimizerParams)
    current_candidates: list[np.ndarray] = field(default_factory=list)
    current_fitnesses: list[float] = field(default_factory=list)
    current_idx: int = 0

    def record_fill(self, outcome: FillOutcome) -> None:
        self.outcomes.append(outcome)
        if len(self.outcomes) > self.max_outcomes:
            self.outcomes = self.outcomes[-self.max_outcomes:]

        if len(self.outcomes) < self.retrain_every:
            return

        if not self.current_candidates:
            self._start_generation()

        if self.current_idx < len(self.current_candidates):
            self.current_fitnesses.append(self._evaluate(outcome.params, outcome))
            self.current_idx += 1

        if self.current_idx >= len(self.current_candidates):
            self._finish_generation()

    def get_params(self) -> OptimizerParams:
        if self.optimizer.generation > 0:
            return self.optimizer.best_params()
        return self.last_params

    def _start_generation(self) -> None:
        self.current_candidates = self.optimizer.ask()
        self.current_fitnesses = []
        self.current_idx = 0

    def _finish_generation(self) -> None:
        improved = self.optimizer.tell(self.current_candidates, self.current_fitnesses)
        if improved:
            self.last_params = self.optimizer.best_params()
        self.current_candidates = []
        self.current_fitnesses = []
        self.current_idx = 0

        if self.optimizer.should_stop() and self.optimizer.generation > 3:
            self.optimizer.reset(self.last_params)

    def _evaluate(self, params: OptimizerParams, outcome: FillOutcome) -> float:
        edge = outcome.edge
        if edge <= 0:
            return edge * 0.5
        vol = 1.0 + abs(outcome.markout) * 10.0
        return edge / vol

    def snapshot(self) -> dict[str, Any]:
        return {
            "generation": self.optimizer.generation,
            "n_outcomes": len(self.outcomes),
            "best_fitness": float(self.optimizer.best_fitness),
            "params": self.get_params().to_profile_overrides(),
        }


# ── multi-market manager ───────────────────────────────────────────────────


class OnlineOptimizerManager:
    """Owns one MarketOptimizer per market, wired into the engine."""

    def __init__(self) -> None:
        self._optimizers: dict[str, MarketOptimizer] = {}

    def get(self, condition_id: str) -> MarketOptimizer:
        if condition_id not in self._optimizers:
            self._optimizers[condition_id] = MarketOptimizer(condition_id=condition_id)
        return self._optimizers[condition_id]

    def record_fill(
        self,
        condition_id: str,
        params: OptimizerParams,
        markout: float,
        fill_size: float,
        fill_price: float,
    ) -> None:
        opt = self.get(condition_id)
        opt.record_fill(FillOutcome(params=params, markout=markout,
                                     fill_size=fill_size, fill_price=fill_price, ts=time.time()))

    def get_params(self, condition_id: str) -> OptimizerParams:
        return self.get(condition_id).get_params()

    def get_profile_overrides(self, condition_id: str) -> dict[str, float]:
        return self.get(condition_id).get_params().to_profile_overrides()

    def snapshot(self, condition_id: str) -> dict[str, Any]:
        return self.get(condition_id).snapshot()

    def remove(self, condition_id: str) -> None:
        self._optimizers.pop(condition_id, None)
