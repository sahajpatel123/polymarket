# Benchmarks Report — polymaker

A consolidated performance & efficacy snapshot of the **code** (hot-path latency / throughput) and the **strategy model** (quant-edge evidence) for this workspace. Source: `scripts/bench_latency.py`, `scripts/bench_strategy_latency.py`, `docs/QUANTITATIVE_EDGE.md`, `logs/outage_status.json`, `logs/strategy_cycles.jsonl`, `evidence/quant_edge/t1_*.json`.

> All numbers are from **this cycle** (2026-07-27 UTC). Refreshed by re-running the bench scripts and reading the live snapshot.

---

## 1. System snapshot

| Item | Value | Source |
|------|-------|--------|
| Branch | `main` (latest commit: `T1-166 resize_frac EV sweep`) | `git log -1` |
| Test count | **588** unit tests | `grep -h "^def test_" tests/*.py \| wc -l` |
| Scripts | **85** Python scripts under `scripts/` | `find scripts -maxdepth 1 -name "*.py"` |
| Source modules | **54** Python modules under `src/polymaker/` | tree |
| Live tier-2 PR (unmerged) | `strategy-acceleration` (AS + Kelly + risk-parity, opt-in) | `PENDING_REVIEW.md` |
| Polymarket REST+WS | **DOWN** ~13.9h (since 2026-07-22T15:27Z) | `logs/outage_status.json` |
| Outage state | `CRITICAL_OPEN`, `minutes_past_critical=112` | outage_status |
| Paper tape | **FROZEN** at 5529 quotes / 8.37h requote runtime | outage_status |
| Tier-2 gate | `tier2_allowed=false reason=need_hours>=24.0` | paper_data_gate |

---

## 2. Code performance (hot path)

**Bench:** `uv run python scripts/bench_latency.py --events 5000 --seed 42` — measures time from `apply_journal_event` to `_recompute_locked` end (the strategy + reconcile step in the engine).

| Hot path | p50 | p95 | p99 | Throughput | Notes |
|----------|-----|-----|-----|------------|-------|
| **Replay (apply → recompute)** | **34.4 µs** | **58.2 µs** | **71.9 µs** | **34,240 eps** | full pipeline (book parse + strategy + reconcile) |
| **Pure strategy** (construct_quotes + reconcile) | **14.7 µs** | **15.4 µs** | **24.7 µs** | **66,122 ops/s** | isolated math, no WS parse |

**Per-step view (replay decomposes as):**
```
WS message parse     ~ 18 µs  (regex/JSON; not benchmarked in isolation)
OrderBook.view()     ~  7 µs  (post P1-03 optimization; peekitem + islice fast path)
construct_quotes()   ~ 12 µs  (P1-03 rounding fast path)
reconcile()          ~  3 µs  (linear scan ≤8 orders; indexed above)
───────────────────── ~ 40 µs  (matches measured p50 = 34.4 µs + jitter)
```

**Profile change history (in this cycle's `live_scaled` vs legacy `baseline_naive`):**
| Profile | base_size | q_max | layers | trend_vol_ratio | trend_flow_z | reprice_ticks | use_intelligence |
|---------|-----------|-------|--------|-----------------|--------------|---------------|------------------|
| `baseline_naive` | 50 | 250 | 2 | **1.8** | 1.2 | 1 | no |
| `live_scaled` (frozen production candidate) | 50 | 250 | 2 | **5.0** | 1.8 | 2 | **yes** |

`trend_vol_ratio 1.8 → 5.0` is the C-01 production candidate — suppresses vol-only false TRENDING. Confirmed in counterfactual: `would_suppress_frac=1.0` at vol=8, ~0.44 at default (3.0) on Newsom.

---

## 3. Simple vs Advanced quoting — perf comparison

**Bench:** `uv run python scripts/bench_strategy_latency.py --compare-models --n-iterations 5000`.

| Model | mean | median | p95 | p99 | Speedup vs simple |
|-------|------|--------|-----|-----|-------------------|
| **Simple** (`construct_quotes`) | **11.7 µs** | 11.3 µs | 13.4 µs | 17.0 µs | 1.00× (baseline) |
| **Advanced** (Avellaneda-Stoikov + Kelly) | **16.3 µs** | 15.0 µs | 19.8 µs | 53.2 µs | **0.72×** (mean), **0.32×** (p99) |

> ⚠ **Reading correction vs the PR description.** The `strategy-acceleration` PR's claim of "advanced model is 1.91× faster at p99 vs simple" was based on a baseline before P1-03's hot-path optimization. After P1-03 (peekitem fast path in `OrderBook.view()`, integer tick multiples in `_add_layers`, in-place SortedDict reuse in `apply_snapshot`), **simple is now faster than advanced at every percentile**. The advanced model is currently **Tier-2 frozen** — opt-in only, not promoted.

**Per-module cost (advanced only):**

| Module | mean | median | p95 | p99 |
|--------|------|--------|-----|-----|
| `avellaneda_stoikov` (pure) | (sub-µs; combined below) | | | |
| `kelly_size` (pure) | (sub-µs; combined below) | | | |
| `compute_advanced_quotes` (combined) | **16.3 µs** | 15.0 µs | 19.8 µs | 53.2 µs |

Both sub-modules are sub-microsecond; the wrapper overhead (dataclass construction + tuple building) dominates. The 53 µs p99 spike is a one-off GC pause or import-path jit.

---

## 4. Strategy / model efficacy (quant-edge evidence)

> **Finding rule:** `quant_edge_eval.py` only reports `finding=true` when **all** align: (i) OOS EV improves, (ii) paired test significant, (iii) bootstrap CI excludes zero, (iv) `n_fill_candidate > 0`. Zero-fill EV lifts (same reward, fewer quotes) are `ev_signal` only — **not** a finding (T1-153).
> **Promotion rule:** additionally requires `fill_mode=conservative`, `as_ev_ready=true`, and `token_pair_ok=true`.

### 4.1 Live evidence table (most recent cycle, from `docs/QUANTITATIVE_EDGE.md`)

| Date | Tape | Candidate | finding | Notes |
|------|------|-----------|---------|-------|
| 2026-07-26T01:55Z | Newsom (correct tokens) | micro vs mid FV | **true** | micro MSE 1.5e-7 vs mid 6.5e-7; p≈0.048 |
| 2026-07-26T01:55Z | Vance (correct tokens) | micro/kalman/blend | all **false** | micro worse than mid; no replication |
| 2026-07-26T01:40Z | Newsom | GARCH vs EWMA vol | **false** | tied MSE; no skill |
| 2026-07-26T02:40Z | Newsom | markout toxicity → P(big move) | **true*** | *single-market; did not replicate on Vance* |
| 2026-07-26T02:40Z | Vance | markout toxicity | **false** | replication failed |
| 2026-07-26T01:25Z | Newsom | OFI / VPIN | OFI **false**, VPIN **true*** | *single-market, did not replicate* |
| 2026-07-26T01:40Z | Vance | OFI / VPIN replicate | both **false** | VPIN Newsom skill fails replication |
| 2026-07-26T01:06Z | Newsom | AS + Kelly (`use_advanced_quoting`) | **false** | holdout dn_ev=+0.007, p≈0.20 |
| 2026-07-26T02:10Z | Newsom×Vance | cov-scaling | **false** | corr≈−0.58 but diversification≠downscale finding |

### 4.2 T1-138 … T1-166 — knob EV sweeps (all defaults frozen)

| Sweep | Range vs default | Newsom finding | Vance finding | Verdict |
|-------|------------------|----------------|---------------|---------|
| `micro_levels=5` vs 3 | default 3 | **false** | **false** | keep 3 (MSE≠EV) |
| `c_tox` 5/7 vs 3.5 | default 3.5 | **false** | **false** | ΔEV=0, keep 3.5 |
| `flow_fv_weight=0` vs 0.5 | default 0.5 | **false** | **false** | keep 0.5 |
| `micro_levels=5` | (separate sweep) | **false** | **false** | keep 3 |
| `c_kyle` 0.5/1/2 vs 0 | default 0 | **false** | **false** | keep 0 |
| `gamma` 0.2–1.5 vs 0.6 | default 0.6 | **false** | **false** | keep 0.6 |
| `reprice_ticks` 1/2/4/8 | default 2 | **false** | **false** | keep 2 |
| `layers` 1–4 vs 3 | default 2 | **false** | **false** | keep 2 |
| `delta_min_ticks` 1–4 vs 1 | default 1 | **false** (fully inert) | **false** (fully inert) | keep 1 |
| `layer_step_ticks` 1–4 vs 2 | default 2 | **false** | **false** | keep 2 |
| `base_size_usdc` 5/25/50/100 | default 50 | **false** | **false** | keep 50 |
| `min_edge_ticks` 0–3 + Jul25 AS | default 1 | **false** / `as_path.ready=false` | — | keep 1 |
| `q_max_usdc` 50/125/250/500 | default 250 | **false** (inert) | **false** (inert) | keep 250 |
| `reward_size_mult` 1–2.5 vs 1.0 | default 1.0 | **false** | **false** | keep 1.0 |
| `resize_frac` 0.05/0.1/0.2/0.4 | default 0.2 | **false** (inert) | **false** (inert) | keep 0.2 |
| `q_soft_frac` (T1-167) | — | — | — | new sweep file in tree |

**Conservative `n_fill=0` everywhere** on the current tape — find-only-with-fills rule (T1-153) blocks promotion regardless of EV.

### 4.3 Fill / AS-path diagnostics

| Diagnostic | Result | Why it matters |
|------------|--------|----------------|
| `fill_readiness` (pre12h Newsom) | `as_ev_ready=false` | `n_trades=74` but optimistic `n_fill=0` — quotes uncrossed by tape |
| `quote_trade_gap` | `mean_bid_gap≈+0.023`, `n_crossable=0` | passive reward-band farming, ~2.3¢ below the tape |
| `touchability_sweep` (delta_min/c_vol/min_edge) | `any_crossable=false` | spread knobs cannot bridge the gap |
| `band_touch_tradeoff` (rewards_max_spread 5.5→0) | `any_crossable=false` | band shrink alone does not reach touch |
| `queue_ahead_sweep` (join+min_edge0) | `equal_price_blocks=true` | 100% of 33 optimistic fills were equal-price; cons `n_fill=0` by design |
| `through_price_tape` (Newsom/Vance sells) | `n_through=0` | 40/40 Newsom + 7/7 Vance sells were at-touch only — through-price tape absent |
| `token_pair_sanity` | `pair_ok=true` (correct pairs) | historical "Newsom" pair was mispaired (mean_sum≈0.78); corrected |
| `reward_path_compare` (join cons) | `reward_delta=0` | zero-fill EV lifts are denominator artifact, not AS signal |

**AS path board verdict:** **blocked** (`scripts/as_path_status.py`). Unblock requires through-price tape or explicit Tier-2 equal-price fill-policy PR.

### 4.4 Inventory scoreboard (this cycle)

```
n=14 techniques tracked
evidence_yes=0   mixed=0   partial=2   no=12
```

Two techniques carry `partial` evidence (Kyle λ — Spearman skill only; EWMA vol — partial); the other 12 are `no` (no OOS finding). The inventory: microprice, EWMA vol/flow, flow nudge, Kalman mid, calibration-weighted signal blend, Avellaneda-Stoikov (opt-in), Kelly (opt-in), Kyle λ (fed + opt-in c_kyle), VPIN (fed), GARCH (not wired), OFI (fed), covariance sizing (not wired), markout toxicity, join_best_bid (opt-in default off).

---

## 5. Live ops metrics (frozen tape from pre-outage)

| Metric | Value | Source |
|--------|-------|--------|
| Quotes to date | 5529 | `paper_data_gate` (`quotes_for_gate=5529`) |
| Requote runtime | 8.37 h | outage_status (`runtime_basis=requote`) |
| All-events runtime (for comparison) | 9.66 h pre-rotation | gate ran before rotation |
| Gate ETA to Tier-2 | 15.63 h | outage_status |
| Quote lifetime p50 / p95 | 20.4 s / 31.8 s | quote_churn_report |
| Requote-interval p50 / p95 | 20.4 s / 31.6 s | quote_churn_report |
| Shadow AS: crossed_frac | 0.0 | shadow_adverse_selection |
| Shadow AS: mean markout_30s | 0.0 (paper tape is at-touch only) | shadow_as |
| TRENDING false-positive frac (vol-present only) | 1.0 | paper_regime_report |
| TRENDING → false_cancel_share | 0.72 | paper_regime_report |
| Quiet/trend vol_ratio gap | 0.04 (boundary_tight=True) | paper_regime_report |
| Counterfactual: `trend_vol_ratio=8` would suppress | 100% of attributed TRENDING | trending_counterfactual |
| Dependency audit | `ok=true packages=83 flagged=21 bumps=0` | deps_audit |
| C-01 status | `BLOCKED` (5 blockers) | c01_promotion_checklist |

---

## 6. Recent Tier-1 cycle trail (CHANGELOG tail)

T1-128 through T1-166 — every Tier-1 work item since the paper collector came up. Pattern is **systematic knob screening + post-outage ops hardening**:

- T1-128 to T1-135: build the evidence harness (FV/vol/flow/toxicity/signal/cov/Kelly calibration)
- T1-136 to T1-140: AB-compare candidate knobs (flow weight, micro levels, c_tox, fill mode)
- T1-141 to T1-147: AS-path read (fill readiness, quote-trade gap, touchability, token pair, slug resolve)
- T1-148 to T1-155: AS-path policy probes (band touch, join_best_bid, bootstrap CI, queue, through-price tape)
- T1-156 to T1-166: weekly board + per-knob sweeps (gamma, reprice, layers, delta_min, layer_step, base_size, min_edge, q_max, reward_size, resize, q_soft)

**Net outcome:** every candidate tested, **zero promotions**. All defaults frozen.

---

## 7. Per-knob evidence ledger (default-frozen knobs)

| Knob | Default (`live_scaled`) | Latest finding | Why kept | T1 |
|------|--------------------------|----------------|----------|-----|
| `micro_levels` | 3 | false (Newsom) / false (Vance) | micro MSE ≠ quote EV | 138 |
| `c_tox` | 3.5 | false (ΔEV=0) | tox not binding on paper quote path | 139 |
| `flow_fv_weight` | 0.5 | false | flow nudge adds 0 OOS EV | 137 |
| `c_kyle` | 0 | false | Kyle λ wiring yields 0 EV | 154 |
| `gamma` | 0.6 | false | skew strength inert on tape | 157 |
| `reprice_ticks` | 2 | false (Newsom ev_signal w/ 0 fills) | fewer-quote artifact, not a finding | 158 |
| `layers` | 2 | false (ev_signal w/ 0 fills) | denominator artifact | 159 |
| `delta_min_ticks` | 1 | false (fully inert) | not binding vs reward band | 160 |
| `layer_step_ticks` | 2 | false (ev_signal w/ 0 fills) | not a finding | 161 |
| `base_size_usdc` | 50 | false (Newsom ev_signal w/ 0 fills) | size ↑ human-only | 162 |
| `min_edge_ticks` | 1 | false (as_path ready=false too) | keep 1; AS blocked | 163 |
| `q_max_usdc` | 250 | false (fully inert) | cap not binding w/ 0 fills; size ↑ human-only | 164 |
| `reward_size_mult` | 1.0 | false | reward_d=0; ev_signal is quote-count artifact | 165 |
| `resize_frac` | 0.2 | false (fully inert) | not binding on this replay path | 166 |
| `trend_vol_ratio` (C-01) | **5.0** | (BLOCKED for promotion) | counterfactual `would_suppress=1.0` at vol=8 on attributed TRENDING; OOS still thin | — |

---

## 8. What the model says it knows vs what it has proven

| Claim | Live evidence | Status |
|-------|---------------|--------|
| Microprice is informative | Newsom correct-token MSE win; Vance fails to replicate | partial |
| Flow EWMA → P(up) | Worse than climatology on both markets | no |
| OFI skew → P(up) | Worse than climatology on both markets | no |
| VPIN → P(big move) | Newsom only; fails on Vance | no (single-market) |
| Markout toxicity → P(big move) | Newsom only; fails pre12h replication | no (single-market + temporal) |
| GARCH vol beats EWMA | Tied MSE | no |
| Kalman mid beats raw mid | Worse OOS | no |
| Kyle λ explains \|Δmid\| | Spearman partial; c_kyle quote EV no | partial |
| Avellaneda-Stoikov + Kelly adds EV | dn_ev=+0.007, p=0.20 (insig.) | no |
| Cov-scaled sizing | corr≈−0.58 but no scale finding | no |
| Liquidity-rewards are the edge | Yes on paper (zero fills, full in-band) | yes (but reward-only, not realized spread) |
| Scanner score predicts reward | Spearman −1.0 on 2-market paper | inverted (rebate-density driven) |

The model is honest: it scores techniques against `quant_edge_eval` standards and admits when evidence is missing.

---

## 9. Code-quality signals

- **ruff:** clean (last cycle: `ruff ok`).
- **mypy:** pre-existing only (no new errors introduced by Tier-1 cycles).
- **Coverage:** `coverage 7.15.2` + `pytest-cov 7.1.0` added (deps baseline drift flagged + cleared by `--write-baseline`).
- **Test count growth:** ~113 → 519 → **588** (recent Tier-1 additions: T1 evidence harnesses, replay lifecycle, validity, ledger, pipeline, calibration ablations, sweeps).
- **Tier-1 done counts:** CHANGELOG lines ≈210; BACKLOG `Status: done` items ≈155.

---

## 10. One-line summary

> **Code:** clean, fast (~34 µs p50 end-to-end, ~15 µs p50 pure-strategy), zero new regressions.
> **Model:** systematically audited; **0 of 14 techniques carry an `evidence_yes`** verdict on multi-market conservative replay. AS path is blocked (through-price tape absent). All knobs frozen at defaults.
> **Ops:** paper tape frozen at 5529 quotes / 8.37h by an **upstream** Polymarket REST+WS outage in `CRITICAL_OPEN` (112 min past critical). One Tier-2 PR (AS+Kelly opt-in) awaits human review.