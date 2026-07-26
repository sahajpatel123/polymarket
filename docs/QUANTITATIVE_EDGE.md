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
| Microprice | `marketdata/orderbook.py` | yes | partial (in live path; needs OOS EV pack) |
| EWMA vol / flow | `strategy/estimators.py` | yes | partial |
| Kalman mid | `intelligence/signal_processing.py` | intel-only | no |
| Calibration-weighted signal blend | `strategy/signal_blend.py` | **no** | no |
| Avellaneda–Stoikov | `strategy/avellaneda_stoikov.py` | opt-in (`use_advanced_quoting`) | no |
| Fractional Kelly | `strategy/kelly.py` | opt-in | no |
| Kyle λ / Glosten–Milgrom | `strategy/kyle_lambda.py` | **fed** (engine+replay); not in quotes | no |
| VPIN | `strategy/vpin.py` | **fed** (engine+replay); not in quotes | partial (OOS Brier skill vs climatology on Newsom; no quote EV yet) |
| GARCH(1,1) vol | `strategy/garch.py` | **no** | no |
| OFI skew | `strategy/ofi.py` | **fed** (engine+replay); not in quotes | no (worse than climatology on Newsom OOS) |
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
| 2026-07-26T01:25Z | livecfg Newsom (signal_calibration) | OFI P(up) / VPIN P(big move) | OFI **false**, VPIN **true*** | *predictive Brier vs tune climatology only — not yet quote-path EV evidence. Kyle Spearman(\|Δmid|)≈0.74 |
