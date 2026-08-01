# 4-Hour Paper Backtest Summary Report

**Generated:** 2026-08-01  
**Starting Capital:** $50.00  
**Backtest Duration:** 4 hours (synthetic journal with 480 trades, 240 book snapshots)

---

## Executive Summary

All backtests were run with **conservative fill mode** (queue-ahead + latency assumptions) — the only mode suitable for financial claims. The synthetic journal generates realistic price action but the market structure (wide spreads, deep books) creates a challenging environment for market making.

**Key Finding:** No profile achieves a positive **honest PnL (conservative rewards)** across all random seeds. The `baseline_naive` profile shows the best performance (+43% daily return) but with only 8 fills (below the 10-fill minimum for statistical validity). The scalping profiles (`scalp-tiny`, `scalp-hot`) suffer from significant adverse selection (negative markouts).

---

## Results by Profile

| Profile | Fills | Quotes | Fill Rate | Spread PnL | Conservative Rewards | Total PnL (Conservative) | Daily Return | Financial Pass? |
|---------|-------|--------|-----------|------------|---------------------|--------------------------|--------------|-----------------|
| **scalp-tiny (seed 42)** | 19 | 509 | 3.73% | $0.15 | $0.32 | **-$2.14** | **-25.69%** | ❌ INSUFFICIENT_DATA |
| **scalp-tiny (seed 1)** | 25 | 651 | 3.84% | $0.25 | $0.33 | **-$3.54** | **-42.47%** | ❌ INSUFFICIENT_DATA |
| **scalp-tiny (seed 2)** | 35 | 700 | 5.00% | $0.21 | $0.33 | **-$7.89** | **-94.74%** | ❌ INSUFFICIENT_DATA |
| **scalp-tiny (seed 3)** | 35 | 719 | 4.87% | $0.40 | $0.33 | **-$7.72** | **-92.60%** | ❌ INSUFFICIENT_DATA |
| **scalp-hot (seed 42)** | 28 | 692 | 4.05% | $0.80 | $0.33 | **-$11.95** | **-143.43%** | ❌ INSUFFICIENT_DATA |
| **aggressive_30 (seed 42)** | 2 | 258 | 0.78% | $0.25 | $0.01 | **$0.25** | **+3.06%** | ❌ n_fill<10 |
| **aggressive_30 (seed 1)** | 2 | 245 | 0.82% | $0.44 | $0.00 | **-$0.54** | **-6.48%** | ❌ n_fill<10 |
| **baseline_naive (seed 42)** | 8 | 748 | 1.07% | $9.98 | $0.28 | **+$3.58** | **+43.00%** | ❌ n_fill<10 |
| **live-tiny (seed 42)** | 2 | 135 | 1.48% | $0.41 | $0.23 | **-$0.19** | **-2.29%** | ❌ n_fill<10 |
| **political-longdated (seed 42)** | 3 | 317 | 0.95% | $2.12 | $0.28 | **+$2.40** | **+28.79%** | ❌ n_fill<10 |
| **political-hot (seed 42)** | 5 | 270 | 1.85% | $4.43 | $0.22 | **-$4.28** | **-51.40%** | ❌ n_fill<10 |
| **romania-pm (seed 42)** | 16 | 473 | 3.38% | $5.75 | $0.33 | **-$7.35** | **-88.14%** | ❌ INSUFFICIENT_DATA |
| **live_scaled (seed 42)** | 22 | 862 | 2.55% | $12.24 | $0.33 | **-$18.58** | **-222.95%** | ❌ INSUFFICIENT_DATA |

---

## Detailed Analysis

### Scalp-Tiny Profile (Multi-Seed)
- **Consistently negative** across all 4 seeds
- Adverse selection (markout) is the killer: -0.015 to -0.037 (30s)
- Without rewards (AS-adjusted): -$3.87 to -$8.23 over 4 hours
- The small order sizes ($2 base, $6 q_max) get toxic fills
- **Verdict:** Not viable with current synthetic market structure

### Scalp-Hot Profile
- Larger sizes ($9 base, $40 q_max) but still negative
- More fills (28) but worse adverse selection
- Without rewards: -$12.28 over 4 hours
- **Verdict:** Worse than scalp-tiny due to larger toxic fills

### Baseline-Naive Profile
- **Best honest PnL** (+$3.58 conservative, +43% daily)
- But only 8 fills — statistically insignificant
- Spread PnL is positive ($9.98) due to wider spreads
- Uses aggressive reprice (1 tick) which causes churn
- **Verdict:** Promising but needs more fills for validation

### Political Profiles
- **political-longdated**: +28.79% with 3 fills (spread PnL $2.12)
- **political-hot**: -51.40% with 5 fills (toxic markout -0.03)
- The wider spreads in political-longdated help avoid toxic fills
- **Verdict:** political-longdated concept works but needs more activity

### Live Profiles (Real Journal Data)
When run against the **real 21-hour journal** (livecfg/journal/paper.jsonl):

| Profile | Markets | Fills | Total Est PnL | Daily Return | Notes |
|---------|---------|-------|--------------|--------------|-------|
| live-tiny | 2 | 0 | $6.58 | +15.07% | Reward-only, 0 fills |
| live_scaled | 2 | 0 | $6.58 | +15.07% | Reward-only, 0 fills |

**Key Insight:** Real journal has only **59 trade prints** over 21 hours — extremely thin. No fills occurred because the books were wide-spread and the trade prints didn't cross our quotes. The PnL estimate is purely **reward accrual** (monopoly assumption).

---

## Risk Assessment

### Financial Validity Gates (All Failed)
1. **Monopoly rewards only positive** — Spread PnL negative or zero in most runs
2. **AS-adjusted spread negative** — Markouts show adverse selection
3. **Insufficient fills** — Most profiles < 10 fills (minimum for statistical validity)
4. **Monopoly only share near zero** — Our quote share of reward pool is small

### What This Means
- **No profile passes the financial validity gate** for promotion to live
- The synthetic market structure (1-tick spread, deep books) penalizes small-order scalping
- Real market data is too thin to generate fills
- The fill model (ML-based adverse selection filter) is trained but not yet deployable (needs live validation)

---

## Recommendations

### 1. **Market Structure Mismatch**
The synthetic journal uses 1-tick spreads with massive depth (1000-20000 size). This is unrealistic for Polymarket where spreads are typically 2-5 ticks with thinner touch. Consider:
- Tighter book generation with 2-5 tick spreads
- More realistic depth at touch (~$50-500 not $10000+)
- Higher trade frequency at touch

### 2. **Scalping Profile Tuning**
For the scalping profiles to work:
- Increase `min_edge_ticks` to ensure we only quote with genuine edge
- Add `join_best_bid = true` to queue at touch (already in scalp-tiny)
- Reduce `c_tox` and `c_vol` to tighten spreads
- Increase `exit_urgency_s` to exit toxic positions faster

### 3. **Fill Model Deployment**
The fill model (`models/fill_model.pkl`) exists but:
- Requires `min_auc = 0.55` and `min_markout_corr = 0.05` on live data
- Needs `min_live_validation_samples = 200` online fills
- Currently runs in shadow mode only
- **Action:** Run paper mode longer to accumulate live validation samples

### 4. **Real Journal Collection**
The current journal has only 59 trades over 21 hours. Need:
- More active markets (politics during events, sports during games)
- Longer collection period (72h+)
- Multiple market categories simultaneously

### 5. **Profile Selection for Live**
Based on these results, the **baseline_naive** or **political-longdated** profiles show the most promise for spread capture, but they need:
- Higher fill rates (more active markets)
- Proper fill model deployment to filter toxic fills
- Realistic market structure for validation

---

## Next Steps

1. **Fix synthetic journal generator** to match real Polymarket microstructure
2. **Run 12h+ paper collection** on live markets to get real fills
3. **Validate fill model** on live data (needs 200+ online samples)
4. **Re-tune scalp profiles** for realistic spreads (2-5 ticks)
5. **Run A/B comparison** between baseline_naive and live_scaled with real data

---

## Appendix: Backtest Commands Used

```bash
# Scalp-tiny multi-seed
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile scalp-tiny --duration-hours 4 --out-dir backtest_4hr_scalp --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile scalp-tiny --duration-hours 4 --out-dir backtest_4hr_scalp_seed1 --config-dir config --seed 1
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile scalp-tiny --duration-hours 4 --out-dir backtest_4hr_scalp_seed2 --config-dir config --seed 2
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile scalp-tiny --duration-hours 4 --out-dir backtest_4hr_scalp_seed3 --config-dir config --seed 3

# Other profiles
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile scalp-hot --duration-hours 4 --out-dir backtest_4hr_scalp_hot --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile aggressive_30 --duration-hours 4 --out-dir backtest_4hr_aggressive_30 --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile baseline_naive --duration-hours 4 --out-dir backtest_4hr_baseline_naive --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile live-tiny --duration-hours 4 --out-dir backtest_4hr_live_tiny --config-dir livecfg --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile political-longdated --duration-hours 4 --out-dir backtest_4hr_political_longdated --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile political-hot --duration-hours 4 --out-dir backtest_4hr_political_hot --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile romania-pm --duration-hours 4 --out-dir backtest_4hr_romania_pm --config-dir config --seed 42
uv run python scripts/run_24h_backtest.py --bankroll 50 --profile live_scaled --duration-hours 4 --out-dir backtest_4hr_live_scaled --config-dir config --seed 42

# Real journal
uv run python scripts/backtest.py --journal livecfg/journal/paper.jsonl --profile live-tiny --config-dir livecfg --out-dir backtest_real_journal --bankroll 50
uv run python scripts/backtest.py --journal livecfg/journal/paper.jsonl --profile live_scaled --config-dir livecfg --out-dir backtest_real_journal_scaled --bankroll 50
```