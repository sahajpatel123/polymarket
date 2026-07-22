# Strategy candidates (Agent-1 scratchpad)

Tooling index: [STRATEGY_AGENT_TOOLING.md](STRATEGY_AGENT_TOOLING.md).

Evidence-backed ideas awaiting the Tier-2 paper gate (≥24h runtime).
Do **not** merge pricing changes from this file without a PR + holdout proof.

## C-01 Raise `trend_vol_ratio` on `live-tiny` (churn / false TRENDING)

- **Hypothesis:** TRENDING trips with `flow_z≈0` are vol-ratio noise on thin
  books; raising `trend_vol_ratio` cuts self-churn without changing FV math.
- **Trade-off:** less adverse-selection protection if a real vol spike arrives
  without flow confirmation (skews toward staying quoted).
- **In-sample (full live tape, newsom market, 2026-07-22 ~1h):**
  `trend_vol_ratio` 2→8 → `dn_quote=-24`, `dn_cancel=-24`.
- **OOS holdout (last 30% events, market-filtered):** `dn_quote=0` → **not
  replicated** (holdout still quote-thin: 4 baseline quotes). Validator:
  `oos_replicated=false`, `thin_holdout=true`.
- **Next:** re-validate after ≥24h paper / denser adverse windows; if OOS still
  fails, reject or combine with higher `trend_flow_z` instead.
- **Status:** `watching` (blocked on data, not code)
- **Update 2026-07-22T08:27Z:** TRENDING still fires late in session (~84m)
  with `flow_z≈-0.08` — vol-ratio path remains the smoking gun, but OOS
  promotion still blocked.
- **Update 2026-07-22T09:08Z (~2.1h tape):** validate events-split still
  `oos_replicated=false` (full_dn_quote=-42, holdout_dn_quote=0, thin_holdout).

## C-02 Prefer higher-reward market weight (selection)

- **Hypothesis:** live scorecard shows one market earning ~2× reward/hour of
  the other under the same `live-tiny` profile — ranking should weight
  realized reward accrual, not only scanner density.
- **Evidence (2026-07-22 ~1.6h paper):** `rank_vs_realized.py` →
  `spearman_scanner_vs_realized=-1.0`, disagreements=2.
  Scanner rank 1 = Newsom (~$8.9/h realized); realized rank 1 = Vance (~$12.8/h).
- **Decomposition:** both markets stayed ~100% in-band; realized gap is almost
  pure `rewards_daily_rate` (308 vs 214). Scanner score still ranks Newsom
  higher — driven by **rebate_potential** (93 vs 10 from volume_24hr), while
  paper metrics currently accrue liquidity rewards only (0 fills → rebate not
  realized). `reward_density` is nearly tied (0.044 vs 0.043).
- **Status:** `watching` (needs multi-day window + fills before retuning
  score_market; n=2 and paper-no-fill regime bias the comparison)
- **Update 2026-07-22T09:18Z:** `spearman_scanner_vs_liquidity_oracle=-1.0`
  matches realized ranks under zero-fill / full in-band — scanner is not wrong
  for rebate-inclusive ranking, but it is the wrong objective for current paper
  evidence (liquidity rewards only).
