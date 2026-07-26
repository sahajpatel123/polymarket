# Quantitative Edge — technique inventory & evidence standard

Supplement to the Strategy & Pricing loop. Objective: maximize **expected
value per quote net of adverse selection**, measured with proper scoring
rules — not classification accuracy.

## Evidence standard (non-negotiable)

A technique only counts as **implemented** when backtested via the harness
with all three:

1. **Calibration metric** — Brier and/or log-loss on probability estimates
   (never raw accuracy / hit-rate).
2. **Out-of-sample validation** — holdout window the parameters were not
   tuned against.
3. **Significance** — bootstrap CI or paired test on the PnL/EV delta;
   a point estimate smaller than sample noise is not a finding.

Harness: `scripts/quant_edge_eval.py` → `polymaker.replay.quant_edge`.
Signal-only scoring: `scripts/signal_calibration.py` → `polymaker.replay.signal_calibration`
(Brier vs tune climatology for OFI/VPIN; not a substitute for quote EV evidence).
Vol forecasting: `scripts/vol_calibration.py` → `polymaker.replay.vol_calibration`
(GARCH vs EWMA OOS MSE + significance).
FV predictors: `scripts/fv_calibration.py` → `polymaker.replay.fv_calibration`
(mid vs microprice vs Kalman vs blend; OOS MSE + significance; `--horizons` multi;
`--sweep-levels` for micro depth).
Micro depth EV: `scripts/micro_levels_ev_sweep.py` (quote EV of alternate
`micro_levels` vs profile default; OOS+CI).
Flow: `scripts/flow_calibration.py` → `polymaker.replay.flow_calibration`
(flow_z → P(up) vs climatology).
Toxicity: `scripts/toxicity_calibration.py` → `polymaker.replay.toxicity_calibration`.
Toxicity spread EV: `scripts/c_tox_ev_sweep.py` (quote EV of alternate `c_tox`).
Kelly fraction: `scripts/kelly_fraction_sweep.py` (requires `StrategyProfile.kelly_fraction`).
Status board: `scripts/quant_edge_status.py`.
Covariance sizing: `scripts/cov_sizing_eval.py` → `polymaker.replay.cov_sizing_eval`
(tune cov vs uncorrelated budget; holdout variance reduction CI).

```bash
uv run python scripts/quant_edge_eval.py \
  --journal fixtures/regime_dense.jsonl \
  --candidate-overrides '{"use_advanced_quoting": true}'
```

Verdict `finding=true` only when OOS EV improves, the paired test is
significant, and the bootstrap CI excludes zero.

`promotion_eligible` additionally requires `--fill-mode conservative`
(default) **and** `as_ev_ready` from the fill-readiness gate (enough
`last_trade_price` prints; optional optimistic fill probe). `base` /
`optimistic` are diagnostics when queue-ahead yields `n_fill≈0` under
conservative — a diagnostic finding alone does not promote.

```bash
uv run python scripts/fill_readiness.py \
  --journal livecfg/journal/paper.jsonl \
  --yes-token … --probe-optimistic
```

Quote–trade gap (why fills stay zero):
```bash
uv run python scripts/quote_trade_gap.py --journal … --yes-token …
uv run python scripts/touchability_sweep.py --journal … --yes-token …
uv run python scripts/token_pair_sanity.py --journal … --yes-token … --no-token …
uv run python scripts/quote_side_coverage.py --journal … --yes-token … --no-token …
```

## Calibration target (important)

FV is a YES-price probability, **not** P(price goes up). Metrics now score
FV against the **future mid/FV at +30s** as a soft label in [0, 1]
(quadratic / soft-label log-loss). Treating FV as P(up) was a misspecified
scoring rule and is no longer used.

## Technique inventory

| Technique | Module | Live/replay wiring | Evidence gate |
|-----------|--------|--------------------|---------------|
| Microprice | `marketdata/orderbook.py` | yes | **no** (correct-token Newsom OOS MSE fails; prior Newsom win was mispaired tokens) |
| EWMA vol / flow | `strategy/estimators.py` | yes | partial (vol); **flow_z directional: no**; **flow nudge in FV: no** (worsens micro OOS MSE); knob `flow_fv_weight` (default 0.5) |
| Kalman mid | `intelligence/signal_processing.py` | intel-only | **no** (worse than mid on Newsom+Vance OOS) |
| Calibration-weighted signal blend | `strategy/signal_blend.py` | **no** | no (no clear OOS win vs mid) |
| Avellaneda–Stoikov | `strategy/avellaneda_stoikov.py` | opt-in (`use_advanced_quoting`) | no |
| Fractional Kelly | `strategy/kelly.py` | opt-in (`kelly_fraction`, default 0.25) | **no** (0.125/0.5 vs 0.25: no EV finding on Newsom) |
| Kyle λ / Glosten–Milgrom | `strategy/kyle_lambda.py` | **fed**; not in quotes | **partial** (Spearman vs \|Δmid\| on correct tokens; not quote EV) |
| VPIN | `strategy/vpin.py` | **fed**; not in quotes | **no** (Newsom Brier skill did not replicate on Vance) |
| GARCH(1,1) vol | `strategy/garch.py` | **no** | **no** (OOS MSE ≈ EWMA on Newsom; finding=false) |
| OFI skew | `strategy/ofi.py` | **fed**; not in quotes | no (worse than climatology) |
| Covariance sizing | `strategy/covariance_sizing.py` | **no** | no |
| Markout toxicity | `strategy/estimators.py` | yes (spreads/size) | **no** (correct-token Newsom+Vance Brier finding=false; prior Newsom win contaminated) |
| Proper scoring + CI | `strategy/calibration.py` | metrics analyze | harness ready |

## Why each exists (one line)

- **Microprice** — book imbalance informs fair value beyond the naive mid.
- **Kalman / EWMA** — track FV through tick noise without overreacting.
- **Calibration-weighted blend** — don't average unequal external vs book signals.
- **Avellaneda–Stoikov** — reservation price + optimal spread from inventory risk × vol.
- **Kyle λ / VPIN** — widen when flow is informed; adverse-selection defense.
- **GARCH / EWMA vol → spreads** — principled vol-responsive width.
- **OFI** — short-horizon skew ahead of likely moves.
- **Fractional Kelly + covariance** — size on edge/variance; never full Kelly; respect correlation.

## Loop policy

Tier-2 wiring of any technique into the live quote path requires a green
`quant_edge_eval` finding on a fresh paper window (gate: ≥24h, ≥500 quotes)
and a PR — never auto-merge. See `AUTONOMOUS_LOOP_PROTOCOL.md`.

## Live evidence log

| When (UTC) | Tape | Candidate | finding | Notes |
|------------|------|-----------|---------|-------|
| 2026-07-26T01:06Z | livecfg Newsom journal | `use_advanced_quoting=true` vs `live_scaled` | **false** | holdout dn_ev=+0.007, OOS sign match, but paired p≈0.20 — not significant |
| 2026-07-26T00:53Z | fixtures/regime_dense | AS+Kelly on | true (synth only) | does not promote; live gate required |
| 2026-07-26T01:25Z | livecfg Newsom (signal_calibration) | OFI P(up) / VPIN P(big move) | OFI **false**, VPIN **true*** | *single-market; see Vance replication row |
| 2026-07-26T01:40Z | livecfg Vance (+pre12h) | OFI / VPIN replicate | both **false** | VPIN Newsom skill **fails replication** → not a finding |
| 2026-07-26T01:40Z | livecfg Newsom (vol_calibration) | GARCH vs EWMA MSE | **false** | n=858; MSE tied; no significant skill |
| 2026-07-26T01:55Z | livecfg Newsom (fv_calibration) | micro vs mid | **true** | micro MSE 1.5e-7 vs mid 6.5e-7; p≈0.048; CI>0 (after CI precision fix) |
| 2026-07-26T01:55Z | livecfg Vance (fv_calibration) | micro / kalman / blend | all **false** | micro worse than mid on Vance — mixed replication |
| 2026-07-26T02:10Z | Newsom multi-horizon FV | micro @ 5/30/120s | **true** at 30s+120s | 5s lower MSE but not significant; strengthens Newsom micro case |
| 2026-07-26T02:10Z | Vance multi-horizon FV | micro @ 5/30/120s | all **false** | still no Vance replication |
| 2026-07-26T02:10Z | Newsom×Vance cov_sizing | corr-scaled notionals | **false** | tune corr=0 (no scale); holdout corr≈−0.58 material but diversification ≠ downscale finding |
| 2026-07-26T02:10Z | Vance AS+Kelly quant_edge | use_advanced_quoting | **false** | holdout dn_ev>0 but p≈0.29 — not significant |
| 2026-07-26T02:25Z | Newsom micro_levels sweep | levels 1–8 @30s | **true** (best **5**) | levels=5 MSE 6.0e-8 p≈0.019; default 3 also true but worse; level 1 not sig |
| 2026-07-26T02:25Z | Vance micro_levels sweep | levels 1–8 @30s | all **false** | micro worse than mid at all depths — do not change default yet |
| 2026-07-26T02:25Z | Newsom+Vance flow_calibration | flow_z → P(up) | both **false** | worse than climatology (like OFI) |
| 2026-07-26T02:40Z | Newsom toxicity_calibration | virtual markout → P(big move) | **true** | Brier skill vs climatology; needs Vance+EV before quote changes |
| 2026-07-26T02:40Z | Vance toxicity_calibration | virtual markout | **false** | no replication |
| 2026-07-26T02:40Z | Newsom kelly_fraction_sweep | 0.125 / 0.25 / 0.5 | all **false** | no EV delta vs quarter-Kelly on this tape |
| 2026-07-26T02:55Z | Newsom FV + flow nudge | micro vs micro+flow | flow_helps=**false** | micro_flow MSE worse than plain micro (sig); live weight=0.5 hurts forecasts |
| 2026-07-26T02:55Z | Newsom toxicity pre12h | temporal replicate | **false** | prior Newsom toxicity finding does not hold on earlier window |
| 2026-07-26T03:10Z | Newsom quant_edge | `flow_fv_weight=0` vs 0.5 (`live_scaled`) | **false** | holdout dn_ev=0; p≈0.69; n_quote≈60 (with correct tick/cid) |
| 2026-07-26T03:10Z | Vance quant_edge | `flow_fv_weight=0` vs 0.5 | **false** | holdout dn_ev=0; no OOS EV lift |
| 2026-07-26T03:25Z | Newsom+Vance quant_edge | `micro_levels=5` vs 3 | both **false** | Newsom full dn_ev negative; Vance holdout dn_ev negative; MSE≠EV |
| 2026-07-26T03:40Z | Newsom+Vance c_tox_ev_sweep | c_tox 5.0/7.0 vs 3.5 | both **false** | ΔEV identically 0 — toxicity not binding on paper quote path |
| 2026-07-26T03:55Z | Newsom fill_mode plumbing | conservative→base/optimistic | infra | n_fill still ~0–1 even optimistic; AS EV remains thin on this tape |
| 2026-07-26T04:10Z | Newsom/Vance/pre12h fill_readiness | as_ev_ready gate | **false** all | Newsom n_trades=5; pre12h n_trades=74 but optimistic n_fill=0 (quotes uncrossed) |
| 2026-07-26T04:25Z | Newsom pre12h quote_trade_gap | bids vs tape | **n_crossable=0** | mean_bid_gap≈+0.023; 36 sell aggressors still miss bids — not fill-sim bug |
| 2026-07-26T04:40Z | Newsom pre12h touchability_sweep | delta_min/c_vol/min_edge | **any_crossable=false** | gap≈0.023 invariant — spread knobs not the bottleneck |
| 2026-07-26T04:55Z | token_pair_sanity | wrong vs catalog Newsom/Vance | **pair_ok** | Historical “Newsom” pair mean_sum=0.78 (Vance NO+Newsom YES); correct pairs sum=1.0, two-sided quotes restore |
| 2026-07-26T05:10Z | correct-token rerun | AS / flow0 / FV micro | all **false** | pair_ok; two-sided quotes; still n_fill=0 / gap≈0.023; **micro finding overturned** |
| 2026-07-26T05:25Z | correct-token calibrations | OFI/VPIN/tox/flow/FV Vance | all **false** | prior VPIN/tox Newsom wins overturned; Kyle Spearman only (partial) |

## Freeze list (do not Tier-2 wire without multi-market EV)

- flow_z / OFI / VPIN / GARCH / Kalman / cov sizing / AS+Kelly — evidence **no** or single-market only
- micro_levels=5, toxicity-aware spreads — **mixed** MSE/toxicity only; **EV micro5=false**; **c_tox EV inert** → keep defaults
- `flow_fv_weight=0` — forecast MSE favors zero on Newsom, but **EV finding=false** on Newsom+Vance; keep default 0.5; knob exposed for further tape
- **AS EV paused**: fill_readiness false; quote_trade_gap shows **bids ~2.3¢ below tape** (n_crossable=0) even with 36 sell aggressors — not a matcher bug
- **CONTAMINATION**: prior live evals using yes=78633590…/no=54533043… mispaired Vance NO with Newsom YES (mean_sum≈0.78); treat those AS/EV/gap results as invalid
- Correct Newsom: yes=54533043… no=87854174… (cid 0x0f49db97…); Vance: yes=40081275… no=78633590… (cid 0x18b1c135…)
- `promotion_eligible` now requires `token_pair_ok` (YES+NO mid sum ≈ 1)
- Correct-token rerun (T1-145): AS/flow0/micro all finding=false; micro prior win **overturned**
- AS EV still blocked (n_fill_optimistic=0); need tape that crosses resting bids
