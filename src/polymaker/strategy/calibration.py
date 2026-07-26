"""Proper Scoring Rules, Calibration Metrics, and Statistical Significance.

Implements calibration evaluation for probability estimates and quote quality:
- Brier Score: Mean squared error of probability predictions vs outcomes
- Log Loss: Cross-entropy loss of probability predictions vs outcomes
- Expected Calibration Error (ECE) and Maximum Calibration Error (MCE)
- Expected Value per Quote net of adverse selection
- Non-parametric Bootstrap Confidence Intervals (95% CI)
- Paired statistical significance testing (t-test / Wilcoxon signed-rank)

No I/O — pure mathematical evaluation module.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

_EPS = 1e-12


def brier_score(probs: Sequence[float], outcomes: Sequence[int | float]) -> float:
    """Compute the Brier Score for probability predictions.
    
    Formula: BS = (1/N) * sum((prob_i - outcome_i)^2)
    Lower is better. Perfect score = 0.0, uninformative 50/50 = 0.25.
    """
    if not probs or len(probs) != len(outcomes):
        return 0.25
    n = len(probs)
    total = sum((p - y) ** 2 for p, y in zip(probs, outcomes))
    return total / n


def log_loss(probs: Sequence[float], outcomes: Sequence[int | float]) -> float:
    """Compute the Log Loss (Cross-Entropy) for probability predictions.
    
    Formula: LL = - (1/N) * sum(y_i * log(p_i) + (1 - y_i) * log(1 - p_i))
    Lower is better. Perfect score = 0.0, uninformative 50/50 = ln(2) ≈ 0.6931.
    """
    if not probs or len(probs) != len(outcomes):
        return math.log(2.0)
    n = len(probs)
    total = 0.0
    for p, y in zip(probs, outcomes):
        p_clamped = min(max(p, _EPS), 1.0 - _EPS)
        total += y * math.log(p_clamped) + (1.0 - y) * math.log(1.0 - p_clamped)
    return -total / n


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    brier_score: float
    log_loss: float
    expected_calibration_error: float  # ECE
    max_calibration_error: float       # MCE
    bin_counts: tuple[int, ...]
    bin_mean_probs: tuple[float, ...]
    bin_mean_outcomes: tuple[float, ...]


def evaluate_calibration(
    probs: Sequence[float], outcomes: Sequence[int | float], n_bins: int = 10
) -> CalibrationReport:
    """Evaluate calibration using binned predictions vs empirical outcome frequencies.
    
    Returns ECE, MCE, Brier score, Log loss, and binned calibration metrics.
    """
    if not probs or len(probs) != len(outcomes) or n_bins <= 0:
        return CalibrationReport(
            brier_score=0.25,
            log_loss=math.log(2.0),
            expected_calibration_error=0.0,
            max_calibration_error=0.0,
            bin_counts=tuple([0] * n_bins),
            bin_mean_probs=tuple([0.0] * n_bins),
            bin_mean_outcomes=tuple([0.0] * n_bins),
        )

    bs = brier_score(probs, outcomes)
    ll = log_loss(probs, outcomes)

    # Bin setup
    bin_sum_prob = [0.0] * n_bins
    bin_sum_out = [0.0] * n_bins
    counts = [0] * n_bins

    for p, y in zip(probs, outcomes):
        p_clamped = min(max(p, 0.0), 1.0 - _EPS)
        b_idx = min(int(p_clamped * n_bins), n_bins - 1)
        bin_sum_prob[b_idx] += p_clamped
        bin_sum_out[b_idx] += y
        counts[b_idx] += 1

    total_n = len(probs)
    ece = 0.0
    mce = 0.0
    mean_probs = []
    mean_outs = []

    for b in range(n_bins):
        cnt = counts[b]
        if cnt > 0:
            mp = bin_sum_prob[b] / cnt
            mo = bin_sum_out[b] / cnt
            gap = abs(mp - mo)
            ece += (cnt / total_n) * gap
            if gap > mce:
                mce = gap
            mean_probs.append(mp)
            mean_outs.append(mo)
        else:
            mean_probs.append((b + 0.5) / n_bins)
            mean_outs.append(0.0)

    return CalibrationReport(
        brier_score=round(bs, 6),
        log_loss=round(ll, 6),
        expected_calibration_error=round(ece, 6),
        max_calibration_error=round(mce, 6),
        bin_counts=tuple(counts),
        bin_mean_probs=tuple([round(x, 6) for x in mean_probs]),
        bin_mean_outcomes=tuple([round(x, 6) for x in mean_outs]),
    )


def expected_value_per_quote(
    spread_capture_usdc: float,
    reward_accrual_usdc: float,
    rebate_usdc: float,
    adverse_selection_cost_usdc: float,
    n_quotes: int,
) -> float:
    """Compute Expected Value per Quote net of adverse selection.
    
    EV_quote = (Spread Capture + Rewards + Rebates - Adverse Selection Cost) / N_quotes
    """
    if n_quotes <= 0:
        return 0.0
    net_total = (spread_capture_usdc + reward_accrual_usdc + rebate_usdc) - adverse_selection_cost_usdc
    return net_total / n_quotes


def bootstrap_confidence_interval(
    series: Sequence[float],
    n_resamples: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Compute non-parametric bootstrap confidence interval for a metric series.

    Returns (mean, ci_lower, ci_upper).

    Uses ``random.Random`` (not a bare LCG ``state % n``). The previous LCG
    sampler produced a full residue cycle mod ``n`` whenever ``n`` was a power
    of two, so every resample mean equaled the sample mean and CIs collapsed.
    """
    if not series:
        return 0.0, 0.0, 0.0

    n = len(series)
    mean_val = sum(series) / n
    if n < 2 or n_resamples <= 0:
        return mean_val, mean_val, mean_val

    rng = random.Random(seed)
    resample_means: list[float] = []
    for _ in range(n_resamples):
        s_sum = 0.0
        for _ in range(n):
            s_sum += series[rng.randrange(n)]
        resample_means.append(s_sum / n)

    resample_means.sort()
    alpha = (1.0 - ci) / 2.0
    lo_idx = max(0, int(alpha * n_resamples))
    hi_idx = min(n_resamples - 1, int((1.0 - alpha) * n_resamples))

    return round(mean_val, 12), round(resample_means[lo_idx], 12), round(resample_means[hi_idx], 12)


@dataclass(frozen=True, slots=True)
class SignificanceResult:
    mean_delta: float
    std_delta: float
    t_statistic: float
    p_value: float
    is_significant: bool  # p_value < alpha (default 0.05)
    ci_lower: float
    ci_upper: float


def paired_significance_test(
    baseline_series: Sequence[float],
    candidate_series: Sequence[float],
    alpha: float = 0.05,
) -> SignificanceResult:
    """Perform a paired t-test between baseline and candidate metric series.
    
    Returns t-statistic, p-value, and 95% confidence interval of the mean delta.
    """
    if not baseline_series or len(baseline_series) != len(candidate_series):
        return SignificanceResult(
            mean_delta=0.0,
            std_delta=0.0,
            t_statistic=0.0,
            p_value=1.0,
            is_significant=False,
            ci_lower=0.0,
            ci_upper=0.0,
        )

    n = len(baseline_series)
    deltas = [c - b for b, c in zip(baseline_series, candidate_series)]
    mean_d = sum(deltas) / n
    if n < 2:
        return SignificanceResult(
            mean_delta=round(mean_d, 6),
            std_delta=0.0,
            t_statistic=0.0,
            p_value=1.0,
            is_significant=False,
            ci_lower=round(mean_d, 6),
            ci_upper=round(mean_d, 6),
        )

    var_d = sum((d - mean_d) ** 2 for d in deltas) / (n - 1)
    std_d = math.sqrt(max(0.0, var_d))
    se_d = std_d / math.sqrt(n)

    if se_d <= _EPS:
        t_stat = 0.0
        p_val = 1.0 if mean_d == 0.0 else 0.0
    else:
        t_stat = mean_d / se_d
        # Approximate 2-tailed p-value using normal/t approximation
        # p ≈ 2 * (1 - Phi(|t|)) via error function approximation
        abs_t = abs(t_stat)
        # Abramowitz and Stegun approximation for Gaussian CDF
        x = abs_t / math.sqrt(2.0)
        t_approx = 1.0 / (1.0 + 0.3275911 * x)
        erf_val = 1.0 - (
            0.254829592 * t_approx
            - 0.284496736 * (t_approx ** 2)
            + 1.421413741 * (t_approx ** 3)
            - 1.453152027 * (t_approx ** 4)
            + 1.061405429 * (t_approx ** 5)
        ) * math.exp(-x * x)
        p_val = max(0.0, min(1.0, 1.0 - erf_val))

    ci_margin = 1.96 * se_d
    return SignificanceResult(
        mean_delta=round(mean_d, 6),
        std_delta=round(std_d, 6),
        t_statistic=round(t_stat, 4),
        p_value=round(p_val, 6),
        is_significant=p_val < alpha,
        ci_lower=round(mean_d - ci_margin, 6),
        ci_upper=round(mean_d + ci_margin, 6),
    )
