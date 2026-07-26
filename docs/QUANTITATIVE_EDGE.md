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
Flow: `scripts/flow_calibration.py` → `polymaker.replay.flow_calibration`
(flow_z → P(up) vs climatology).
Covariance sizing: `scripts/cov_sizing_eval.py` → `polymaker.replay.cov_sizing_eval`
(tune cov vs uncorrelated budget; holdout variance reduction CI).

```bash
uv run python scripts/quant_edge_eval.py \
  --journal fixtures/regime_dense.jsonl \
  --candidate-overrides '{"use_advanced_quoting": true}'
```

Verdict `finding=true` only when OOS EV improves, the paired test is
significant, and the bootstrap CI excludes zero.

## Calibration target (important)

FV is a YES-price probability, **not** P(price goes up). Metrics now score
FV against the **future mid/FV at +30s** as a soft label in [0, 1]
(quadratic / soft-label log-loss). Treating FV as P(up) was a misspecified
scoring rule and is no longer used.

## Technique inventory

| Technique | Module | Live/replay wiring | Evidence gate |
|-----------|--------|--------------------|---------------|
| Microprice | `marketdata/orderbook.py` | yes | **mixed** (Newsom OOS yes, best depth=5; Vance fails all depths) |
| EWMA vol / flow | `strategy/estimators.py` | yes | partial (vol); **flow_z directional: no** |
| Kalman mid | `intelligence/signal_processing.py` | intel-only | **no** (worse than mid on Newsom+Vance OOS) |
| Calibration-weighted signal blend | `strategy/signal_blend.py` | **no** | no (no clear OOS win vs mid) |
| Avellaneda–Stoikov | `strategy/avellaneda_stoikov.py` | opt-in (`use_advanced_quoting`) | no |
| Fractional Kelly | `strategy/kelly.py` | opt-in | no |
| Kyle λ / Glosten–Milgrom | `strategy/kyle_lambda.py` | **fed**; not in quotes | mixed (Spearman vs \|Δmid\| unstable across windows) |
| VPIN | `strategy/vpin.py` | **fed**; not in quotes | **no** (Newsom Brier skill did not replicate on Vance) |
| GARCH(1,1) vol | `strategy/garch.py` | **no** | **no** (OOS MSE ≈ EWMA on Newsom; finding=false) |
| OFI skew | `strategy/ofi.py` | **fed**; not in quotes | no (worse than climatology) |
| Covariance sizing | `strategy/covariance_sizing.py` | **no** | no |
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
