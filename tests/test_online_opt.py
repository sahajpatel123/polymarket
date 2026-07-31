"""Tests for Pillar 3: Online parameter optimization via CMA-ES."""

import numpy as np

from polymaker.strategy.online_opt import (
    CMAESOptimizer,
    FillOutcome,
    MarketOptimizer,
    OnlineOptimizerManager,
    OptimizerParams,
)


def test_optimizer_params_to_profile_overrides():
    p = OptimizerParams(
        delta_min_ticks=3.7,
        layer_step_ticks=2.1,
        flow_fv_weight=0.6,
        gamma=0.15,
        c_vol=3.0,
        c_tox=20.0,
    )
    overrides = p.to_profile_overrides()
    assert overrides["delta_min_ticks"] == 4  # rounded to int
    assert overrides["layer_step_ticks"] == 2
    assert abs(overrides["gamma"] - 0.15) < 0.001


def test_optimizer_params_clamp():
    p = OptimizerParams(delta_min_ticks=100.0, gamma=-1.0)
    p.clamp()
    assert p.delta_min_ticks <= 8.0
    assert p.gamma >= 0.0


def test_cmaes_ask_tell():
    np.random.seed(42)
    opt = CMAESOptimizer(sigma=0.2)
    solutions = opt.ask()
    # λ = 4 + int(3 * ln(9)) = 4 + 6 = 10
    assert len(solutions) == 10
    assert len(solutions) == opt._lambda


def test_cmaes_converges_on_simple_objective():
    """Test that CMA-ES finds the optimum of a simple quadratic."""
    np.random.seed(42)
    opt = CMAESOptimizer(sigma=0.5)

    def objective(params):
        target = np.array([2.0, 1.0, 0.5, 0.1, 3.0, 10.0, 1.0, 1.0, 0.25])
        return -np.sum((params - target) ** 2)

    for gen in range(30):
        solutions = opt.ask()
        fitnesses = [objective(s) for s in solutions]
        opt.tell(solutions, fitnesses)
        if opt.should_stop():
            break

    best = opt.best_params()
    assert abs(best.delta_min_ticks - 2.0) < 2.0  # converged near optimum
    assert opt.generation > 0


def test_market_optimizer_records_fills():
    mo = MarketOptimizer(condition_id="test", retrain_every=5)
    p = OptimizerParams()
    for i in range(6):
        mo.record_fill(FillOutcome(params=p, markout=0.001, fill_size=10.0, fill_price=0.5, ts=i))
    assert len(mo.outcomes) == 6


def test_manager_get_and_record():
    mgr = OnlineOptimizerManager()
    params = OptimizerParams()
    mgr.record_fill("cid1", params, markout=0.002, fill_size=10.0, fill_price=0.5)
    mgr.record_fill("cid1", params, markout=-0.001, fill_size=5.0, fill_price=0.5)
    opt = mgr.get("cid1")
    assert opt.condition_id == "cid1"
    assert len(opt.outcomes) == 2


def test_manager_remove():
    mgr = OnlineOptimizerManager()
    params = OptimizerParams()
    mgr.record_fill("cid1", params, 0.0, 1.0, 0.5)
    assert mgr.get("cid1").condition_id == "cid1"
    mgr.remove("cid1")


def test_fill_outcome_edge():
    outcome = FillOutcome(
        params=OptimizerParams(), markout=0.002, fill_size=100.0, fill_price=0.5, ts=0.0,
    )
    assert outcome.edge > 0  # positive markout → positive edge
    assert abs(outcome.edge - 0.2) < 0.001


def test_fill_outcome_adverse():
    outcome = FillOutcome(
        params=OptimizerParams(), markout=-0.005, fill_size=50.0, fill_price=0.5, ts=0.0,
    )
    assert outcome.edge < 0
