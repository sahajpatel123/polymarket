# Strategy candidates (Agent-1 scratchpad)

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

## C-02 Prefer higher-reward market weight (selection)

- **Hypothesis:** live scorecard shows one market earning ~2× reward/hour of
  the other under the same `live-tiny` profile — ranking should weight
  realized reward accrual, not only scanner density.
- **Evidence:** `reward_scorecard.py` top ~$12.8/h vs lower sibling.
- **Status:** `watching` (needs multi-day window + T2-01 PR)
