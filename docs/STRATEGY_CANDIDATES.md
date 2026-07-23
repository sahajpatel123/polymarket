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
- **Update 2026-07-22T12:55Z (~5.9h tape, evidence pack):** per-market
  `trend_vol_ratio` 2→8 still cuts churn in-sample (`full_dn_quote=-93` /
  `-14`) but holdouts remain thin (`thin_holdout=true`,
  `oos_replicated=false`). **Do not promote.** Shadow AS (YES-space):
  `crossed_frac≈0`, `markout_30s≈0` on ~3960 quote lifetimes — resting
  post-only quotes are not being mid-crossed in this quiet window; churn
  reduction is the only in-sample signal so far.
- **Update 2026-07-22T14:08Z (~7.1h tape):** evidence pack still
  `oos_replicated=false` / `thin_holdout=true` (full_dn_quote=-111 / -14).
  Holdout remains too thin to promote despite larger in-sample churn cut.
- **Update 2026-07-22T14:20Z (dense synth offline):**
  `fixtures/regime_dense.jsonl` (8 cycles) clears `thin_holdout` for
  `trend_vol_ratio` validate, but `oos_replicated` still false
  (holdout_dn_quote=0). Confirms the idea needs adverse windows that
  actually replicate — not just more events.
- **Update 2026-07-22T14:30Z:** `paper_regime_report --trend-flow-z 1.2` on
  livecfg → `false_trending_frac=1.0` (all TRENDING requotes have
  `|flow_z|<1.2`). Smoking gun for vol-ratio-only trips; promotion still
  blocked on OOS holdout quality.
- **Update 2026-07-22T14:40Z (~7.7h):** false_trending still 1.0;
  `false_trending_cancel_share≈0.72` (most cancels sit on those trips) while
  place share ≈0.02. Evidence pack: full_dn_quote −117/−15, holdout −3/0,
  still `thin_holdout` / `oos_replicated=false`. Do not promote.
- **Update 2026-07-22T14:50Z:** requote logs now emit `vol_ratio` (T1-41) so
  future TRENDING can be split flow_only vs vol_only vs both. Legacy tape
  lacks the field (`missing_vol`); attribution fills in after collector
  restart. Still no Tier-2 merge.
- **Update 2026-07-22T15:10Z (~8.1h):** dual-knob validate
  `trend_vol_ratio=8` + `trend_flow_z=2.0` matches vol-only screen
  (full_dn −126/−17, holdout 0, thin). Raising flow_z adds nothing on a
  100% vol_only TRENDING tape — C-01 remains a pure `trend_vol_ratio` lever.
- **Update 2026-07-22T15:20Z (~8.3h, frozen journal):** evidence pack still
  `oos_replicated=false` / `thin_holdout=true` (holdout_base_nq≈6–7). A live
  append race briefly looked like OOS=true — packs now freeze the journal
  before validate (T1-44). **Do not promote.**
- **Update 2026-07-22T15:50Z:** Polymarket REST+WS unreachable from collector
  host (handshake/URL timeouts). Paper health STALE; quotes frozen ~5529 /
  ~8.7h. Gate ETA paused (T1-46/T1-47). Not a strategy issue — wait for
  upstream recovery before more C-01 live validates.
- **Update 2026-07-22T16:00Z (offline counterfactual, outage):** on attributed
  TRENDING requotes (`vol_ratio` present), `trend_vol_ratio=8` +
  `trend_flow_z=1.2` would suppress **100%** (20/20, cancel_sum=24);
  threshold 5 suppresses 75%. Reinforces C-01 without needing live WS.
  Promotion still blocked on 24h gate + non-thin OOS replay.
- **Update 2026-07-22T16:10Z (per-market sweep):** at flowz=1.2,
  suppress_frac by `trend_vol_ratio` — Newsom `3→0.44, 5→0.81, 8→1.0`;
  Vance `3→0.25, 5→0.50, 8→1.0`. Both markets fully clear vol-only TRENDING
  at 8; 5 is partial. Still no Tier-2 merge (gate + OOS).
- **Update 2026-07-22T16:20Z:** evidence pack now embeds the counterfactual
  sweep (`c01_counterfactual`); outage-friendly `--skip-validate` path.
  Latest pack: both markets suppress_frac=1.0 at vol=8.
- **Update 2026-07-22T16:30Z:** Polymarket outage open >1h (quotes still
  5529 / ~8.7h). Use `await_polymarket_recovery.py` on recovery; cycle
  append can `--skip-connectivity` meanwhile.
- **Update 2026-07-22T16:40Z:** gate runtime no longer pads on outage noise
  (`get_full_book_failed` / `market_ws_dropped`). Live:
  `runtime_hours(requote)=8.37` vs `all_events=9.66` (T1-52).
- **Update 2026-07-22T17:00Z:** `c01_promotion_checklist` → BLOCKED on
  `hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin`
  (quotes already OK). No Tier-2 PR until Polymarket recovers + denser OOS.
- **Update 2026-07-22T17:10Z:** attributed `vol_ratio` dist — QUIET
  p90≈1.22 / max≈1.99; TRENDING p50≈3.11 / max≈7.58; quiet→trend gap≈0.04.
  Default threshold 2.0 sits on the boundary; 8 clears all current TRENDING
  (max&lt;8). Still no merge.
- **Update 2026-07-22T17:20Z:** checklist now surfaces `vol_context`
  (`boundary_tight=True`, quiet_max≈1.989, trend_min≈2.029). Still BLOCKED.
- **Update 2026-07-22T17:40Z:** attributed false-TRENDING (vol-present only)
  still 100%; informational `suggested_vol≈2.489` (quiet_max+0.5). Candidate
  target remains 8 for full suppress; no Tier-2 PR while outage continues.
- **Update 2026-07-22T18:20Z:** CF sweep on frozen tape —
  suppress@2=0, @suggested(2.489)≈0.19, @8=1.0. Soft floor only cuts ~19% of
  attributed TRENDING; target 8 still full suppress. Still no merge.
- **Update 2026-07-22T20:55Z:** Polymarket outage now **~5.5h**
  (`outage_alert_severe=True`). Quotes still frozen at 5529 / runtime 8.37h.
  Compact status at `logs/outage_status.json`. Still no Tier-2 PR.
- **Update 2026-07-22T21:10Z:** `await_polymarket_recovery` now refreshes
  `outage_status.json` on each probe (STILL_DOWN) and marks `recovered` on UP.
  Still no Tier-2 PR while UP is DOWN.
- **Update 2026-07-22T21:20Z:** `outage_status` now includes
  `hours_to_tier2_gate` (24−runtime_h) and live `connectivity` on probe.
  Still ~15.6h gate remaining on frozen 8.37h requote runtime.
- **Update 2026-07-22T21:30Z:** `outage_status` preserves connectivity across
  rewrites and embeds `tier2_allowed` / `gate_reason` from paper_data_gate.
  Outage ~6h; still no Tier-2 PR.
- **Update 2026-07-22T21:40Z:** WEEKLY_REPORT embeds `outage_status` snapshot
  (hours_to_tier2_gate / tier2_allowed). Still DOWN ~6.2h; no Tier-2 PR.
- **Update 2026-07-22T21:50Z:** cycle trail now embeds `outage_status` per row;
  summarize surfaces hours_to_tier2_gate. Still DOWN ~6.3h; no Tier-2 PR.
- **Update 2026-07-22T22:00Z:** `validate_outage_status` checks monitor schema;
  strategy_tick reports `outage_status=OK`. Still DOWN ~6.5h; no Tier-2 PR.
- **Update 2026-07-22T22:10Z:** every `strategy_tick` runs `deps_audit`
  (`deps_ok` / bumps into outage_status). Still DOWN ~6.7h; no Tier-2 PR.
- **Update 2026-07-22T22:20Z:** docs corrected — `political-longdated` /
  `political-hot` are present in `config/strategy.toml`. Still DOWN ~6.8h.
- **Update 2026-07-22T22:30Z:** `outage_status` now carries `tape_frozen` /
  `last_requote_age_s`. Outage crossed **~7h**; still no Tier-2 PR.
- **Update 2026-07-22T22:40Z:** requote age now comes from live `paper_health`
  (was frozen from an old cycle row). Still DOWN ~7.2h; no Tier-2 PR.
- **Update 2026-07-22T22:50Z:** WEEKLY_REPORT now auto-counts Tier-1 changelog
  lines + backlog done. Still DOWN ~7.3h; no Tier-2 PR.
- **Update 2026-07-22T23:00Z:** WEEKLY_REPORT counts `PENDING_REVIEW` rows
  (`pending_reviews=0`). Still DOWN ~7.5h; no Tier-2 PR.
- **Update 2026-07-22T23:10Z:** `outage_status` now includes collector
  `ensure_status` + `collector_pid` (alive but STALE). Still DOWN ~7.7h.
- **Update 2026-07-22T23:20Z:** documented `outage_status` field contract;
  validator recommends health/ensure/deps/tape keys. Still DOWN ~7.8h.
- **Update 2026-07-22T23:30Z:** `outage_alert_prolonged=True` (≥8h). Loop tick
  100; quotes still 5529 / runtime 8.37h; no Tier-2 PR.
- **Update 2026-07-22T23:40Z:** `outage_alert_prolonged` is now a required
  `outage_status` key. Still DOWN ~8.2h; no Tier-2 PR.
- **Update 2026-07-22T23:50Z:** `outage_status` includes `n_cycles` from the
  strategy cycle trail. Still DOWN ~8.3h; no Tier-2 PR.
- **Update 2026-07-23T00:00Z:** `outage_status` includes `c01_status` /
  `c01_blockers`. Still DOWN ~8.5h; no Tier-2 PR.
- **Update 2026-07-23T00:10Z:** fixed gate after overnight `paper.jsonl`
  rotation — discovery now includes dated archives so requote runtime stays
  ~8.37h. Still DOWN ~8.7h; no Tier-2 PR.
- **Update 2026-07-23T00:20Z:** `outage_status.paper_log` shows the selected
  archive (`paper.jsonl.2026-07-22`). Still DOWN ~8.8h; no Tier-2 PR.

- **Update 2026-07-23T00:35Z (T1-98):** Gate/health now union active+rotated ``paper.jsonl`` so midnight rotation cannot freeze the 24h clock after recovery. Still blocked on outage (~9h DOWN, quotes=5529).

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

## C-03 Multi-knob null screen (~6h tape, 2026-07-22T13:00Z)

Offline `validate_knob_candidate` on both livecfg markets (events holdout 30%):

| Knob | Best vs live-tiny | full Δn_quote | holdout | OOS |
|------|-------------------|---------------|---------|-----|
| `reprice_ticks` 1/2/4/8 | 1 | 0 / 0 | thin | fail |
| `gamma` 0.4/0.8/1.2 | 0.4 | 0 / 0 | thin | fail |
| `event_jump_ticks` 4/6/10 | 4 | 0 / 0 | thin | fail |
| `c_tox` 1.5/3/6 | 1.5 | 0 | thin | fail |
| `trend_flow_z` 0.8/1.2/2.0 | 0.8 | −8 / −45 | thin / +3 | fail |

- **Takeaway:** on this quiet zero-fill window only regime-entry knobs
  (`trend_vol_ratio` C-01, `trend_flow_z`) move quote counts; sticky-reprice /
  skew / EVENT jump are inert. Do not invent Tier-2 PRs from null screens.
- **Status:** `watching` (await denser adverse windows + fills)

## C-04 Wire unused profile knobs (or delete)

Confirmed unused by `scripts/profile_knob_audit.py` (n_unused=3):

| Knob | Intended role | Risk if wired |
|------|---------------|---------------|
| `exit_urgency_s` | Raise exit urgency with hold time | More aggressive exits → more churn / worse maker edge |
| `end_date_taper_days` | Soft size taper before `reduce_only_hours` | Earlier size cut → less reward uptime |
| `event_sweep_levels` | Named but unused; sweep uses frac/mult only | Wiring without design could false-trip EVENT |

- **Status:** `watching` — Tier-2 only after paper fills + adverse windows;
  prefer implement-or-delete over leaving dead knobs in `strategy.toml`.
- **Update 2026-07-22T19:40Z:** `unused_knob_toml_scan` confirms all three
  dead knobs are set in `livecfg/strategy.toml` (and exit/taper in
  `config/strategy.toml`). Values have **no live effect** until wired or
  removed — do not tune them expecting churn/size changes.
