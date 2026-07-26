"""Unit tests for Quantitative Edge modules:

- Calibration & Proper Scoring Rules (Brier score, Log loss, ECE, EV per quote, significance testing)
- VPIN (Volume-Synchronized Probability of Informed Trading)
- Kyle's Lambda (Glosten-Milgrom price impact estimator)
- Order-Flow Imbalance (OFI)
- Multi-Market Covariance Position Sizing
"""

from __future__ import annotations

import pytest

from polymaker.domain import Side
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.calibration import (
    bootstrap_confidence_interval,
    brier_score,
    evaluate_calibration,
    expected_value_per_quote,
    log_loss,
    paired_significance_test,
)
from polymaker.strategy.covariance_sizing import (
    compute_covariance_matrix,
    scale_correlated_positions,
)
from polymaker.strategy.kyle_lambda import KyleLambdaEstimator
from polymaker.strategy.ofi import OFICalculator
from polymaker.strategy.vpin import VPINEstimator

# ── Calibration & Proper Scoring Rules Tests ─────────────────────────────


def test_brier_score_perfect():
    probs = [1.0, 0.0, 1.0, 0.0]
    outcomes = [1, 0, 1, 0]
    assert brier_score(probs, outcomes) == 0.0


def test_brier_score_uninformative():
    probs = [0.5, 0.5, 0.5, 0.5]
    outcomes = [1, 0, 1, 0]
    assert brier_score(probs, outcomes) == pytest.approx(0.25)


def test_log_loss_perfect():
    probs = [0.999999, 0.000001]
    outcomes = [1, 0]
    loss = log_loss(probs, outcomes)
    assert loss < 0.01


def test_calibration_evaluation():
    probs = [0.1, 0.2, 0.8, 0.9]
    outcomes = [0, 0, 1, 1]
    report = evaluate_calibration(probs, outcomes, n_bins=5)
    assert report.brier_score < 0.05
    assert report.log_loss < 0.3
    assert report.expected_calibration_error <= 0.15


def test_expected_value_per_quote():
    ev = expected_value_per_quote(
        spread_capture_usdc=10.0,
        reward_accrual_usdc=5.0,
        rebate_usdc=2.0,
        adverse_selection_cost_usdc=3.0,
        n_quotes=100,
    )
    # Net = 10 + 5 + 2 - 3 = 14 / 100 = 0.14
    assert ev == pytest.approx(0.14)


def test_bootstrap_confidence_interval():
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    mean, lo, hi = bootstrap_confidence_interval(data, n_resamples=500, ci=0.95, seed=123)
    assert mean == pytest.approx(3.0)
    assert lo <= mean <= hi


def test_bootstrap_ci_nondegenerate_for_power_of_two_n():
    """Power-of-two lengths must not collapse the CI (old LCG residue-cycle bug)."""
    data = [-0.0004, 0.0003, 0.0008, 0.0012, 0.0056, 0.0032, 0.0022, 0.0007]
    mean, lo, hi = bootstrap_confidence_interval(data, n_resamples=1000, ci=0.95, seed=7)
    assert mean == pytest.approx(sum(data) / len(data))
    assert hi - lo > 1e-6
    assert lo < mean < hi


def test_paired_significance_test():
    baseline = [1.0, 1.5, 2.0, 1.2, 1.8]
    candidate = [1.5, 2.2, 2.8, 1.9, 2.5]  # systematically higher
    res = paired_significance_test(baseline, candidate, alpha=0.05)
    assert res.mean_delta > 0
    assert res.t_statistic > 0
    assert res.p_value < 0.05
    assert res.is_significant is True


# ── VPIN Tests ────────────────────────────────────────────────────────────


def test_vpin_estimator_basic():
    vpin = VPINEstimator(bucket_volume=10.0, n_buckets=4)
    assert vpin.vpin == 0.0

    # 10 shares BUY -> fills bucket 1 (100% buy)
    vpin.update(Side.BUY, 10.0)
    assert vpin.vpin == pytest.approx(1.0)

    # 10 shares SELL -> fills bucket 2 (100% sell)
    vpin.update(Side.SELL, 10.0)
    assert vpin.vpin == pytest.approx(1.0)  # absolute imbalance sum / total = (10+10)/20 = 1.0

    # 5 shares BUY + 5 shares SELL -> fills bucket 3 (0% net imbalance)
    vpin.update(Side.BUY, 5.0)
    vpin.update(Side.SELL, 5.0)
    # Buckets: (10 buy, 0 sell: imb=10), (0 buy, 10 sell: imb=10), (5 buy, 5 sell: imb=0)
    # Total imbalance = 20, Total vol = 30 -> VPIN = 20/30 = 0.6667
    assert vpin.vpin == pytest.approx(20.0 / 30.0, abs=1e-3)


# ── Kyle's Lambda Tests ───────────────────────────────────────────────────


def test_kyle_lambda_estimator():
    kyle = KyleLambdaEstimator(halflife_s=60.0)

    # 1st trade at mid=0.50
    kyle.update(mid=0.50, aggressor=Side.BUY, size=100.0, ts=1.0)
    assert kyle.lambda_param == pytest.approx(0.0001)

    # 2nd trade at mid=0.52 (2 cent price move on 100 shares -> 0.02 / 100 = 0.0002)
    kyle.update(mid=0.52, aggressor=Side.BUY, size=100.0, ts=2.0)
    assert kyle.lambda_param > 0.0001
    assert kyle.adverse_selection_spread(100.0) > 0.0


# ── Order-Flow Imbalance (OFI) Tests ──────────────────────────────────────


def test_ofi_calculator():
    ofi = OFICalculator(halflife_s=30.0)

    book1 = BookView(
        best_bid=0.50, best_bid_size=100.0,
        best_ask=0.52, best_ask_size=100.0,
        second_bid=0.49, second_ask=0.53,
        bid_depth=100.0, ask_depth=100.0,
    )
    ofi.update_from_book(book1, ts=1.0)

    # Bids increase at same price -> e_bid = +50
    book2 = BookView(
        best_bid=0.50, best_bid_size=150.0,
        best_ask=0.52, best_ask_size=100.0,
        second_bid=0.49, second_ask=0.53,
        bid_depth=150.0, ask_depth=100.0,
    )
    val = ofi.update_from_book(book2, ts=2.0)
    assert val > 0.0
    assert ofi.normalized_ofi > 0.0



# ── Multi-Market Covariance Sizing Tests ──────────────────────────────────


def test_covariance_sizing():
    # 2 correlated assets
    returns = [
        [0.01, 0.02, -0.01, 0.03, -0.02],
        [0.01, 0.015, -0.008, 0.025, -0.018],
    ]
    cov = compute_covariance_matrix(returns)
    assert len(cov) == 2
    assert cov[0][1] > 0  # positive correlation

    proposed = [100.0, 100.0]
    res = scale_correlated_positions(proposed, cov, max_portfolio_variance=0.001)
    assert len(res.adjusted_notionals) == 2
    assert res.portfolio_variance <= res.max_allowed_variance + 1e-6
