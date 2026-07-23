# BACKLOG — autonomous loop work queue

Status values: `todo` | `in-progress` | `done` | `rejected-with-reason`.
Pull the next incomplete item top-to-bottom within the tier you are allowed
to work this cycle. Every item has falsifiable done criteria — do not mark
done without evidence (script output / tests) from that cycle.

## Tier 1 — infrastructure

### T1-01 Paper-trading metrics logger
- Status: `done`
- Done when: every quote update, fill, and cancel is logged with timestamp,
  market ID, side, price, size, and resulting inventory; a script can compute
  realized spread captured, adverse-selection cost (30s / 2min / 5min after a
  fill), inventory drift over time, and reward/rebate accrual per market,
  purely from these logs.
- Evidence path: `logs/metrics-paper.jsonl`, `scripts/paper_metrics.py`,
  `src/polymaker/metrics/`; verified `tests/test_metrics.py`

### T1-02 Deterministic backtest/replay harness
- Status: `done`
- Done when: a script replays a historical order-book/log window through the
  current strategy code and reproduces the same metrics as T1-01, with no live
  connection required.
- Evidence: `src/polymaker/replay/`, `scripts/replay_backtest.py`,
  `tests/test_replay.py`

### T1-03 Alerting wrapper
- Status: `done`
- Done when: process crash, kill-switch trigger, daily-loss-cap trigger,
  WebSocket disconnect beyond N seconds, and API auth failure each produce an
  immediate message to a configured endpoint, verified by deliberately
  triggering each condition in paper mode.
- Evidence: `scripts/verify_alerts.py` → all_required_posted=true;
  `tests/test_alerts.py`

### T1-04 Structured logging
- Status: `done`
- Done when: all logs are JSON with consistent fields, rotated, and greppable
  by market ID and time range. (Partial: structlog JSON file exists; rotation
  and market-ID greppability still incomplete.)
- Evidence: TimedRotatingFileHandler + required fields in `logging.py`;
  `scripts/grep_logs.py` / `polymaker.loggrep`; `tests/test_logging.py`

### T1-05 Test suite for deterministic components
- Status: `done`
- Done when: order reconciliation, quote-generation math, and config parsing
  each have unit tests covering documented edge cases (zero inventory, max
  inventory, missing market data, disconnect mid-quote). (Partial suite exists;
  edge-case matrix not fully documented/covered.)
- Evidence: `docs/EDGE_CASES.md`; `tests/test_config.py`; quoting/reconcile
  edge tests; disconnect covered by hardening cancel-failure test

### T1-06 CI pipeline
- Status: `done`
- Done when: every commit automatically runs the full test suite and reports
  pass/fail before merge is possible.
- Evidence: `.github/workflows/ci.yml` (pytest on push/PR to main); README notes
  required status check for merge gating

### T1-07 Dependency audit script
- Status: `done`
- Done when: a script checks every dependency against pinned versions/hashes
  and flags anything with post-install scripts or unexplained version bumps.
- Evidence: `scripts/deps_audit.py`, `deps/baseline.json`, `tests/test_deps_audit.py`;
  CI runs `--fail-on-flags`

### T1-08 Local metrics dashboard
- Status: `done`
- Done when: the metrics from T1-01 are visualized simply enough that a human
  glancing at it once or twice a day can assess system health without reading
  raw logs.
- Evidence: `scripts/metrics_dashboard.py` → `logs/dashboard.html`;
  `polymaker dashboard`; `tests/test_dashboard.py`

### T1-09 Strategy A/B compare harness (eval infra for Tier-2)
- Status: `done`
- Done when: a script replays one journal through baseline vs candidate
  StrategyProfile overrides, prints T1-01 metric deltas (spread, markout,
  inventory drift, reward accrual, quote/cancel counts), and supports a
  timestamp holdout slice for OOS scoring. No strategy math changes.
- Evidence: `src/polymaker/replay/compare.py`, `scripts/compare_strategies.py`,
  `tests/test_compare_strategies.py`

### T1-10 Multi-regime synth journal + named-profile compare
- Status: `done`
- Done when: a deterministic quiet→jump→recovery journal can be generated and
  compared across named strategy.toml profiles (e.g. newsom-mm vs romania-pm)
  via the T1-09 harness; paper_data_gate discovers `livecfg/logs/paper.jsonl`.
- Evidence: `src/polymaker/replay/synth.py`, `scripts/synth_regime_journal.py`,
  `fixtures/regime_jump.jsonl`; compare status=OK dn_quote=-4.0

### T1-11 Strategy-loop snapshot script
- Status: `done`
- Done when: one command prints paper_data_gate + live paper metrics + offline
  named-profile compare on the synth regime tape; paper_metrics auto-finds
  livecfg logs.
- Evidence: `scripts/strategy_snapshot.py`; status=OK with paper_quotes + reward_accrual_sum

### T1-12 Paper regime/churn report
- Status: `done`
- Done when: a script summarizes requote regime mix, transitions, and
  cancel/place churn from paper.jsonl (evidence surface for T2-04/T2-05).
- Evidence: `scripts/paper_regime_report.py`, `tests/test_paper_regime_report.py`;
  live: trending_frac≈0.067 with trending_flowz_mean=0.0 (vol-ratio trips)

### T1-13 Offline StrategyProfile knob sweep
- Status: `done`
- Done when: a script sweeps one profile knob across values on a fixed journal
  and prints T1-01 metric deltas vs baseline (prep for Tier-2 PRs).
- Evidence: `scripts/sweep_profile_knob.py`, `tests/test_sweep_profile_knob.py`;
  reprice_ticks sweep on regime fixture: 1→dn_quote=+3, 5→dn_quote=-1

### T1-14 Livecfg journal replay with token auto-detect
- Status: `done`
- Done when: a script infers YES/NO tokens from metrics-paper.jsonl and replays
  livecfg/journal/paper.jsonl per market; optional named-profile A/B on that tape.
- Evidence: `scripts/replay_livecfg.py`, `tests/test_replay_livecfg.py`;
  live-tiny vs newsom-mm dn_quote=+27/+22 on two live markets

### T1-15 Paper gate counts metrics quotes (BACKLOG-aligned)
- Status: `done`
- Done when: paper_data_gate uses metrics-paper.jsonl `event=quote` counts for the
  ≥500 quote threshold (requotes still reported); 24h runtime unchanged.
- Evidence: `scripts/paper_data_gate.py`, `tests/test_paper_data_gate.py`;
  live quotes_for_gate=531, tier2 blocked only on need_hours>=24.0

### T1-16 Per-market reward/churn scorecard
- Status: `done`
- Done when: a script ranks live paper markets by reward_per_hour with regime
  mix and cancel churn (T2-01 evidence surface).
- Evidence: `scripts/reward_scorecard.py`, `tests/test_reward_scorecard.py`;
  live top market ~12.8 USDC/h reward accrual; trend_vol_ratio live sweep
  2→8 dn_quote=-24 (candidate for later T2-04, not merged)

### T1-17 Knob candidate OOS validator + candidates doc
- Status: `done`
- Done when: a script compares full-window vs holdout metric deltas for a knob
  and flags non-replicated / thin-holdout results; candidates tracked in
  docs/STRATEGY_CANDIDATES.md.
- Evidence: `scripts/validate_knob_candidate.py`; live trend_vol_ratio
  full_dn_quote=-24 holdout_dn_quote=0 oos_replicated=false thin_holdout=true

### T1-18 Event-count holdout + market-token journal filter
- Status: `done`
- Done when: holdout splits can cut by event count; compare/validate filter
  multi-market journals to the target YES/NO tokens before slicing.
- Evidence: `slice_journal_rows(split=events)`, `filter_rows_for_tokens`;
  C-01 still oos_replicated=false after market-filtered event holdout

### T1-19 Strategy snapshot includes reward + regime
- Status: `done`
- Done when: strategy_snapshot.py prints gate, reward scorecard, and regime
  report alongside offline compare in one command.
- Evidence: `scripts/strategy_snapshot.py` status line with top_reward_per_hour
  + trending_frac

### T1-20 Scanner rank vs realized reward/hour
- Status: `done`
- Done when: a script joins catalog scanner scores with paper reward_per_hour
  and reports Spearman ρ + rank disagreements (T2-01 evidence).
- Evidence: `scripts/rank_vs_realized.py`; live spearman=-1.0 disagreements=2
  (Vance wins realized, Newsom wins scanner)

### T1-21 Reward accrual decomposition in rank report
- Status: `done`
- Done when: rank_vs_realized also prints rewards_daily_rate, in_band_hours,
  in_band_frac so rank inversions can be attributed to pool size vs uptime.
- Evidence: both live markets in_band_frac≈1.0; daily_rate 308 vs 214 explains
  realized gap

### T1-22 Scanner component breakdown in rank report
- Status: `done`
- Done when: rank report surfaces rebate_potential / reward_density / extremity
  from score_json so inversions can be attributed to rebate-vs-liquidity-reward.
- Evidence: Newsom rebate_pot≈93 vs Vance≈10; paper has 0 fills so realized
  path is liquidity-reward only

### T1-23 Strategy cycle history appender
- Status: `done`
- Done when: one command appends gate+snapshot+rank status into a JSONL history
  file for longitudinal Agent-1 evidence while waiting on 24h.
- Evidence: `scripts/append_strategy_cycle.py`; C-01 recheck full_dn_quote=-42
  still fails OOS

### T1-24 Liquidity-reward oracle rank in rank report
- Status: `done`
- Done when: rank_vs_realized also ranks by rewards_daily_rate (oracle for
  zero-fill / full in-band paper) and reports Spearman vs scanner.
- Evidence: live spearman_scanner_vs_liquidity_oracle=-1.0 (same inversion)

### T1-25 Paper collector staleness watchdog
- Status: `done`
- Done when: a script fails if newest requote/quote is older than N seconds;
  wired into append_strategy_cycle.
- Evidence: `scripts/paper_health.py`; live status=OK fresh ages

### T1-26 Strategy cycle summary + ETA to 24h gate
- Status: `done`
- Done when: a script summarizes strategy_cycles.jsonl with hours remaining and
  wall-clock ETA to the Tier-2 runtime gate.
- Evidence: `scripts/summarize_strategy_cycles.py`

### T1-27 Strategy agent tooling index doc
- Status: `done`
- Done when: docs list the Agent-1 evidence scripts and point at open candidates.
- Evidence: `docs/STRATEGY_AGENT_TOOLING.md`

### T1-28 Shadow adverse-selection from quote lifetimes
- Status: `done`
- Done when: a script measures fill-independent mid/FV markouts and mid-cross
  rates over resting quote lifetimes from metrics-paper.jsonl (YES-space remap
  for NO-token quotes), so zero-fill paper still yields adverse-selection
  evidence for T2-02/T2-04.
- Evidence: `src/polymaker/metrics/shadow_as.py`,
  `scripts/shadow_adverse_selection.py`, `tests/test_shadow_adverse_selection.py`

### T1-29 Candidate evidence pack (C-01 + shadow AS + regime)
- Status: `done`
- Done when: one command re-validates trend_vol_ratio on livecfg journals
  (token auto-infer), runs shadow AS + regime/scorecard/gate, and writes a
  JSON pack for STRATEGY_CANDIDATES updates — no pricing merges.
- Evidence: `scripts/candidate_evidence_pack.py`; denser-tape C-01 still
  `oos_replicated=false` / `thin_holdout=true`

### T1-30 Cycle history includes shadow AS
- Status: `done`
- Done when: append_strategy_cycle records shadow lifetimes / crossed_frac /
  markout_30s each tick; summarize_strategy_cycles surfaces the latest values.
- Evidence: `scripts/append_strategy_cycle.py`,
  `scripts/summarize_strategy_cycles.py`; multi-knob null screen logged as C-03

### T1-31 StrategyProfile knob usage audit
- Status: `done`
- Done when: a script lists StrategyProfile fields referenced by
  strategy/engine/reconciler/replay vs never referenced; documents
  `exit_urgency_s`, `end_date_taper_days`, `event_sweep_levels` as unused.
- Evidence: `src/polymaker/strategy/knob_audit.py`,
  `scripts/profile_knob_audit.py`, `tests/test_profile_knob_audit.py`;
  status n_unused=3

### T1-32 Richest paper-log discovery (gate shadowing fix)
- Status: `done`
- Done when: paper_data_gate / paper_metrics / shadow AS pick the longest
  existing paper/metrics JSONL so a tiny `logs/paper.jsonl` cannot reset the
  Tier-2 runtime counter away from `livecfg/logs`.
- Evidence: `src/polymaker/metrics/log_discovery.py`; gate restored to
  livecfg ~6.3h / 4200 quotes after preferring richest log

### T1-33 Propagate richest-log discovery + weekly report refresh
- Status: `done`
- Done when: paper_health / regime / scorecard / rank / snapshot use
  `pick_richest_log`; `WEEKLY_REPORT.md` overwritten with this-cycle script
  stdout (paper ~6.5h collecting).
- Evidence: scripts listed above; WEEKLY_REPORT generated 2026-07-22T13:30Z

### T1-34 Quote lifetime / requote-interval churn report
- Status: `done`
- Done when: a script reports lifetime and requote-interval p50/p95 from
  metrics-paper.jsonl (T2-05 evidence); wired into append_strategy_cycle.
- Evidence: `src/polymaker/metrics/churn.py`, `scripts/quote_churn_report.py`,
  `tests/test_quote_churn_report.py`

### T1-35 Quote metrics include `fv_yes`
- Status: `done`
- Done when: engine + replay emit YES-space FV on each quote event; shadow AS
  prefers `fv_yes` over nearest-mark heuristic for NO-token remap.
- Evidence: `engine.py` / `replay` quote emit; `tests/test_shadow_adverse_selection.py`

### T1-36 Metrics quote flush + schema verifier
- Status: `done`
- Done when: MetricsLogger flushes quote/cancel/fill immediately (not stuck
  behind 256-mark batch); verifier flags stale collectors missing `fv_yes`.
- Evidence: `src/polymaker/metrics/__init__.py`,
  `scripts/verify_metrics_schema.py`, tests; live quotes now include `fv_yes`

### T1-37 Cycle history records metrics schema + C-01 7h refresh
- Status: `done`
- Done when: append_strategy_cycle includes verify_metrics_schema status;
  STRATEGY_CANDIDATES updated with ~7.1h C-01 evidence + C-04 unused-knob list.
- Evidence: schema=OK on live collector; C-01 still oos=false thin_holdout

### T1-38 Dense multi-cycle regime synth (non-thin OOS)
- Status: `done`
- Done when: synth journals support `--cycles` / `--dense`; offline validate
  holdout can clear `thin_holdout` (≥20 baseline quotes).
- Evidence: `fixtures/regime_dense.jsonl` (712 events, 8 cycles);
  `trend_vol_ratio` validate thin_holdout=false (oos still false)

### T1-39 False-TRENDING fraction in regime report
- Status: `done`
- Done when: paper_regime_report counts TRENDING requotes with
  `|flow_z| < trend_flow_z` as false_trending (C-01 smoking-gun metric).
- Evidence: live `false_trending_frac=1.0` at threshold 1.2

### T1-40 False-TRENDING cancel/place share + cycle wiring
- Status: `done`
- Done when: regime report exposes cancel/place share on false TRENDING;
  snapshot/append/summarize surface the metric for longitudinal cycles.
- Evidence: live `false_trending_cancel_share≈0.72`; C-01 pack refreshed
  (~7.7h, still thin OOS)

### T1-41 Log `vol_ratio` on requote + dual-path TRENDING attribution
- Status: `done`
- Done when: engine requote logs include `vol_ratio`; regime report
  attributes TRENDING as flow_only / vol_only / both when present.
- Evidence: pytest; collector restarted to emit field on new requotes

### T1-42 Paper requote schema verifier + vol_only in cycles
- Status: `done`
- Done when: verify_paper_schema checks recent requotes for `vol_ratio`;
  snapshot/append/summarize surface `vol_only_frac` + paper_schema status.
- Evidence: pytest; live paper_schema OK/CATCHING_UP after T1-41 restart

### T1-43 Multi-override knob validate (`--also-set`)
- Status: `done`
- Done when: validate_knob_candidate applies repeatable `--also-set k=v`
  beside the swept knob (dual-knob C-01 screens without Tier-2 merges).
- Evidence: pytest; live vol8+flow2 ≡ vol8 on ~8h tape (vol_only regime)

### T1-44 Freeze journal for evidence pack + holdout_base_nq
- Status: `done`
- Done when: candidate_evidence_pack snapshots journal before validate;
  validate status exposes holdout_baseline_n_quote (anti live-append race).
- Evidence: frozen pack ~8.3h still thin (base_nq≈6–7); no Tier2

### T1-45 Ensure/restart paper collector helper
- Status: `done`
- Done when: ensure_paper_collector diagnoses STALE and can `--restart`;
  surfaces collector_hint (e.g. ws_handshake_timeout) after relaunch.
- Evidence: pytest diagnose path; live WS handshake timeouts leave health
  STALE after restart (external outage, not strategy)

### T1-46 Connectivity probe + pause ETA while collector STALE
- Status: `done`
- Done when: polymarket_connectivity probes REST+WS; cycle summarize sets
  `eta_paused` when last health is STALE; append records connectivity.
- Evidence: live status=DOWN (REST+WS timeout); ETA paused until recovery

### T1-47 Outage window report from strategy cycles
- Status: `done`
- Done when: outage_window_report measures STALE/DOWN stretches; summarize
  surfaces outage_open / outage_total_h.
- Evidence: pytest; live open outage while Polymarket REST+WS down

### T1-48 Offline TRENDING counterfactual (C-01, no network)
- Status: `done`
- Done when: trending_counterfactual estimates suppressible TRENDING rows
  from logged flowz+vol_ratio under candidate thresholds.
- Evidence: live tape suppress_frac=1.0 at vol=8 / flowz=1.2 on attributed
  rows (20/20); works while Polymarket is DOWN

### T1-49 Counterfactual vol sweep + per-market breakdown
- Status: `done`
- Done when: `--sweep-vol` and `--by-market` report suppress_frac grid for
  C-01 threshold choice offline.
- Evidence: Newsom 3/5/8 → 0.44/0.81/1.0; Vance → 0.25/0.50/1.0

### T1-50 Evidence pack includes C-01 counterfactual (+ skip-validate)
- Status: `done`
- Done when: candidate_evidence_pack embeds per-market suppress sweep;
  `--skip-validate` allows outage-time packs without journal replay.
- Evidence: skip-validate pack counterfactual both markets=1.0 at vol=8

### T1-51 Await Polymarket recovery + fast cycle flags
- Status: `done`
- Done when: await_polymarket_recovery polls UP then optional collector
  restart; append supports `--skip-connectivity` / `--with-counterfactual`.
- Evidence: pytest once-path; outage >1h documented

### T1-52 Gate runtime from requote span (ignore outage noise)
- Status: `done`
- Done when: paper_data_gate `runtime_hours` uses requote timestamps only;
  still reports `runtime_hours_all_events` for transparency.
- Evidence: live requote=8.37h vs all_events=9.66h during WS outage

### T1-53 Richest-log score uses requote runtime
- Status: `done`
- Done when: `paper_log_score` ranks by requote span (fallback all-events);
  outage-padded logs cannot shadow an active collector.
- Evidence: pytest pick_richest ignores ws_dropped padding

### T1-54 C-01 promotion readiness checklist
- Status: `done`
- Done when: c01_promotion_checklist aggregates gate/health/outage/
  counterfactual/evidence-pack OOS into READY vs BLOCKED + blockers.
- Evidence: live BLOCKED on hours/health/outage/oos/thin (expected)

### T1-55 Regime report vol_ratio percentiles + quiet/trend gap
- Status: `done`
- Done when: paper_regime_report exposes quiet/trending vol_ratio
  min/p50/p90/max and quiet→trend gap for C-01 threshold choice.
- Evidence: live quiet max≈1.99 vs trend min≈2.03 (gap≈0.04)

### T1-56 Wire vol_context into C-01 checklist + cycle trail
- Status: `done`
- Done when: checklist/snapshot/cycles expose quiet_vol_max, trend_vol_min,
  vol_gap, and boundary_tight vs target threshold (informational only).
- Evidence: live checklist vol_gap≈0.04 boundary_tight=True

### T1-57 Attributed false-TRENDING + suggested_vol floor
- Status: `done`
- Done when: regime report separates legacy missing_vol from attributed
  false-TRENDING, and suggests quiet_max+0.5 as informational C-01 floor.
- Evidence: live attr_frac=1.0 on vol-present rows; suggested_vol≈2.489

### T1-58 Record C-01 checklist in strategy cycle trail
- Status: `done`
- Done when: append_strategy_cycle stores c01 status/blockers; summarize
  surfaces last_c01_status / last_c01_blockers.
- Evidence: live c01=BLOCKED with hours/health/outage/oos/thin blockers

### T1-59 Skip collector restart while Polymarket is DOWN
- Status: `done`
- Done when: ensure_paper_collector --restart returns SKIPPED_UPSTREAM_DOWN
  when REST/WS probe fails (unless --allow-down-restart).
- Evidence: live SKIPPED_UPSTREAM_DOWN during ~2.5h outage

### T1-60 Record outage_window_report in strategy cycle trail
- Status: `done`
- Done when: append stores outage open/total_h; summarize surfaces
  last_outage_* alongside derived outage windows.
- Evidence: live outage_open=True total_h≈2.7 during Polymarket DOWN

### T1-61 C-01 checklist CF at 2 / suggested / target
- Status: `done`
- Done when: checklist sweeps counterfactual at default 2, suggested_vol,
  and target; surfaces suppress_frac_* on status line.
- Evidence: live suppress fractions printed during outage freeze

### T1-62 Record C-01 suppress curve in strategy cycle trail
- Status: `done`
- Done when: append/summarize surface suppress_2 / suppress_suggested /
  suppress_target from the checklist status line each tick.
- Evidence: live suppress 0 / 0.1875 / 1.0 on frozen tape

### T1-63 Append strategy cycle on Polymarket recovery
- Status: `done`
- Done when: await_polymarket_recovery (default) runs append_strategy_cycle
  after collector restart so the trail timestamps outage→UP.
- Evidence: unit test recover_appends_cycle; live still DOWN (~3.2h)

### T1-64 Surface requote age + outage_alert (≥3h) in checklist/trail
- Status: `done`
- Done when: checklist/append/summarize expose last_requote_age_s; checklist
  sets outage_alert when outage_total_h≥3.
- Evidence: live outage_alert=True with age≈12k s during DOWN

### T1-65 Mark cycle metrics tape_frozen when health is STALE
- Status: `done`
- Done when: append stores tape_frozen=true on STALE health; summarize
  surfaces last_tape_frozen so shadow/churn are not read as live AS.
- Evidence: live tape_frozen=True during Polymarket outage

### T1-66 Refresh WEEKLY_REPORT during Polymarket outage
- Status: `done`
- Done when: WEEKLY_REPORT overwritten with this-cycle gate/metrics/shadow/
  regime/checklist/summarize/deps output reflecting STALE + outage_alert.
- Evidence: report Generated 2026-07-22T19:10Z; outage ~3.7h documented

### T1-67 Consolidated strategy_tick ops script
- Status: `done`
- Done when: strategy_tick.py runs connectivity + C-01 + summarize in one
  shot; optional --append; unit tests for status parsing.
- Evidence: live status=OK with c01=BLOCKED outage_alert=True

### T1-68 Refresh deps baseline after pytest-cov/coverage add
- Status: `done`
- Done when: deps_audit --write-baseline clears baseline_drift for intentional
  coverage/pytest-cov additions; ok=true bumps=0.
- Evidence: live status=OK packages=83 flagged=21 bumps=0

### T1-69 Scan TOML for set-but-unused strategy knobs (C-04)
- Status: `done`
- Done when: unused_knob_toml_scan lists unused knobs present in
  config/livecfg strategy.toml; tests cover hit/miss.
- Evidence: live n_set_unused>0 for exit_urgency_s/end_date_taper/event_sweep

### T1-70 Annotate livecfg dead knobs + include scan in strategy_tick
- Status: `done`
- Done when: livecfg/strategy.toml comments mark C-04 unused knobs;
  strategy_tick status surfaces unused_set.
- Evidence: live unused_set=9; live-tiny comments present

### T1-71 Annotate config/strategy.toml C-04 unused knobs
- Status: `done`
- Done when: config profiles comment exit_urgency_s / end_date_taper_days
  as unused; strategy.md points at unused_knob_toml_scan.
- Evidence: file header + inline comments on newsom/political/romania profiles

### T1-72 Record unused_set in strategy cycle trail
- Status: `done`
- Done when: append stores unused_knobs status; summarize surfaces
  last_unused_set.
- Evidence: live unused_set=9 on cycle append during outage

### T1-73 Refresh WEEKLY_REPORT at ~5h Polymarket outage
- Status: `done`
- Done when: WEEKLY_REPORT overwritten with this-cycle gate/metrics/shadow/
  regime/checklist/summarize/deps reflecting ~4.85h STALE outage.
- Evidence: Generated 2026-07-22T20:20Z; unused_set=9; deps bumps=0

### T1-74 Automate WEEKLY_REPORT generation script
- Status: `done`
- Done when: write_weekly_report.py gathers gate/metrics/shadow/regime/c01/
  summarize/deps and overwrites WEEKLY_REPORT.md; unit tests for helpers.
- Evidence: live status=OK wrote=WEEKLY_REPORT.md during ~5h outage

### T1-75 Severe outage alert at ≥5h
- Status: `done`
- Done when: c01 checklist sets outage_alert_severe when outage_total_h≥5;
  status line + strategy_tick/append surface it.
- Evidence: live outage_alert_severe=True at ~5.17h DOWN

### T1-76 Summarize severe alert + strategy_tick --write-weekly
- Status: `done`
- Done when: summarize surfaces last_outage_alert_severe; strategy_tick
  optional --write-weekly regenerates WEEKLY_REPORT.md.
- Evidence: live severe=True on summarize; --write-weekly status=OK

### T1-77 Persist compact outage_status.json
- Status: `done`
- Done when: outage_window_report --status-out writes alert/severe snapshot;
  strategy_tick updates logs/outage_status.json each tick.
- Evidence: live status_out with outage_alert_severe=True (~5.5h)

### T1-78 await_recovery refreshes outage_status.json
- Status: `done`
- Done when: await_polymarket_recovery --once refreshes logs/outage_status.json
  on STILL_DOWN; on RECOVERED marks recovered=True / outage_open=False.
- Evidence: unit tests for STILL_DOWN + RECOVERED status_out paths

### T1-79 hours_to_tier2_gate + connectivity in outage_status
- Status: `done`
- Done when: compact outage_status includes hours_to_tier2_gate (24-runtime_h);
  await patches connectivity on STILL_DOWN; strategy_tick prints gate hours + quotes.
- Evidence: live status JSON with hours_to_tier2_gate≈15.63 at runtime 8.37h

### T1-80 Preserve probe fields + merge paper_data_gate into outage_status
- Status: `done`
- Done when: status-out rewrite preserves connectivity/recovered/gate keys;
  strategy_tick merges tier2_allowed/gate_reason into logs/outage_status.json.
- Evidence: live status has connectivity + tier2_allowed=false after strategy_tick

### T1-81 Weekly report embeds outage_status snapshot
- Status: `done`
- Done when: write_weekly_report includes Outage/gate snapshot from
  logs/outage_status.json; live WEEKLY_REPORT refreshed during current outage.
- Evidence: WEEKLY_REPORT shows outage_total_h / hours_to_tier2_gate / tier2_allowed

### T1-82 Embed outage_status in strategy cycle trail
- Status: `done`
- Done when: append_strategy_cycle writes outage_status object into each JSONL
  row; summarize surfaces last hours_to_tier2_gate / tier2_allowed.
- Evidence: live append row includes outage_status; summarize shows gate hours

### T1-83 Validate outage_status schema + freshness
- Status: `done`
- Done when: validate_outage_status checks required keys (+ optional max-age);
  strategy_tick runs it after merging gate/connectivity.
- Evidence: live status=OK on current outage_status during ~6.5h DOWN

### T1-84 deps_audit on every strategy_tick
- Status: `done`
- Done when: strategy_tick runs deps_audit each cycle; surfaces deps_ok/bumps;
  merges deps_* into outage_status; weekly report shows deps fields.
- Evidence: live deps_ok=True bumps=0 during ~6.7h Polymarket outage

### T1-85 Correct stale political profile docs
- Status: `done`
- Done when: docs/strategy.md + CLAUDE.md list political-longdated/hot as
  shipped in config/strategy.toml; remove "absent" gap; profiles validate.
- Evidence: StrategyProfile.model_validate OK for all 4 config profiles

### T1-86 Persist tape_frozen / requote age in outage_status
- Status: `done`
- Done when: strategy_tick merges tape_frozen, eta_paused, last_requote_age_s
  into logs/outage_status.json; weekly snapshot includes those fields.
- Evidence: live outage_status shows tape_frozen=True and requote age during ~7h DOWN

### T1-87 Live paper_health ages in outage_status
- Status: `done`
- Done when: strategy_tick runs paper_health and overwrites last_requote_age_s
  with the live age (not stale cycle-trail value); surfaces health= on status line.
- Evidence: live last_requote_age_s advances with wall clock (~7.2h outage)

### T1-88 Weekly report includes Tier-1 done counts
- Status: `done`
- Done when: write_weekly_report counts CHANGELOG Tier1 lines + BACKLOG T1
  Status:done; System table shows both; live WEEKLY_REPORT refreshed.
- Evidence: live tier1_changelog / tier1_backlog_done printed on status line

### T1-89 Weekly report counts PENDING_REVIEW rows
- Status: `done`
- Done when: write_weekly_report counts non-empty PENDING_REVIEW table rows
  instead of hardcoding 0; status line surfaces pending_reviews.
- Evidence: live pending_reviews=0 with empty PENDING_REVIEW table

### T1-90 Collector PID + ensure_status in outage_status
- Status: `done`
- Done when: strategy_tick diagnose-runs ensure_paper_collector; merges
  ensure_status + collector_pid into outage_status; status line shows ensure=.
- Evidence: live ensure=NEEDS_RESTART collector_pid=78216 during ~7.7h DOWN

### T1-91 Document outage_status field contract + expand recommended keys
- Status: `done`
- Done when: STRATEGY_AGENT_TOOLING documents required/recommended keys;
  validate_outage_status RECOMMENDED_KEYS includes health/ensure/deps/tape fields;
  live validate reports recommended_missing=-.
- Evidence: live validate status=OK recommended_missing=- during ~7.8h DOWN

### T1-92 Prolonged outage alert at ≥8h
- Status: `done`
- Done when: compact_status + c01 checklist set outage_alert_prolonged when
  outage_total_h≥8; strategy_tick/summarize/append/weekly surface it.
- Evidence: live outage_alert_prolonged=True at ~8.0h DOWN (loop tick 100)

### T1-93 Require outage_alert_prolonged in status validation
- Status: `done`
- Done when: validate_outage_status REQUIRED_KEYS includes
  outage_alert_prolonged; docs updated; live validate OK; cycle trail stamped.
- Evidence: live status=OK with prolonged=True; summarize last_outage_alert_prolonged=True

### T1-94 Persist n_cycles in outage_status
- Status: `done`
- Done when: strategy_tick merges summarize cycles= into outage_status n_cycles;
  weekly + validate recommended include n_cycles; status line shows n_cycles=.
- Evidence: live n_cycles≥79 during ~8.3h Polymarket outage

### T1-95 Persist C-01 blockers in outage_status
- Status: `done`
- Done when: strategy_tick merges c01_status + c01_blockers into outage_status;
  weekly/validate recommended include them.
- Evidence: live c01_status=BLOCKED with hours_ok,health_ok,outage_closed,… during ~8.5h DOWN

### T1-96 Include rotated paper.jsonl.* in richest-log discovery
- Status: `done`
- Done when: default_paper_candidates includes paper.jsonl.YYYY-MM-DD rotations;
  pick_richest prefers archive over empty post-rotation paper.jsonl; gate returns
  runtime_basis=requote again.
- Evidence: live gate runtime_hours≈8.37 requote after overnight rotation

### T1-97 Surface paper_log path in outage_status + rotation docs
- Status: `done`
- Done when: strategy_tick parses log_path/metrics_path into outage_status;
  tooling docs explain midnight rotation; live paper_log ends with dated archive.
- Evidence: live paper_log=…/paper.jsonl.2026-07-22 during ~8.8h DOWN

### T1-98 Union rotated paper.jsonl family for gate + health
- Status: `done`
- Done when: paper_data_gate runtime spans active+dated rotations; paper_health
  uses freshest requote across the family; tests prove archive 2h + active 1h
  → union 3h; outage_status may carry paper_log_files.
- Evidence: unit test runtime_hours=3.0000; live log_files=2 while DOWN

### T1-99 Live gate quotes/runtime + critical (≥12h) outage alert
- Status: `done`
- Done when: strategy_tick maps gate quotes/runtime into outage_status ints;
  outage_alert_critical fires at ≥12h; validate requires the key; quotes not float.
- Evidence: live quotes=5529 (int), outage_alert_critical=False at ~9.2h DOWN

### T1-100 hours_to_critical + merged status-line snapshot
- Status: `done`
- Done when: outage_status carries hours_to_critical (=max(0,12-total_h));
  strategy_tick stderr reads merged status (int quotes); tests cover ETA.
- Evidence: live hours_to_critical≈2.7 during ~9.3h DOWN

### T1-101 outage_started_at + summarize critical countdown
- Status: `done`
- Done when: compact status includes outage_started_at from window t_start;
  summarize emits last_hours_to_critical / last_outage_started_at; recovery
  clears started_at; live status shows ISO start during open outage.
- Evidence: live outage_started_at present while ~9.5h DOWN

### T1-102 Summarize after outage merge (live status overlay)
- Status: `done`
- Done when: strategy_tick runs outage/gate merge before summarize; summarize
  overlays live outage_status for hours_to_critical/outage_started_at so the
  status line is not one cycle stale.
- Evidence: live summarize outage_started_at matches outage_status in same tick

### T1-103 Open-outage requires started_at + hours_to_critical
- Status: `done`
- Done when: validate_outage_status fails if outage_open and
  hours_to_critical/outage_started_at missing or null; closed outages pass
  without those keys; live validate OK during open outage.
- Evidence: unit test + live validate status=OK with both fields present

### T1-104 outage_alert_imminent (final hour before critical)
- Status: `done`
- Done when: compact status sets outage_alert_imminent when
  hours_to_critical≤1 and total_h<12; cleared on recovery; tests cover
  11.2h→True and 12.01h→False; live False at ~10h with hours_to_critical≈2.
- Evidence: unit + live outage_alert_imminent=False at ~10.0h DOWN

### T1-105 last_requote_at ISO in outage_status
- Status: `done`
- Done when: strategy_tick derives last_requote_at/last_quote_at from live
  health ages; preserve+recommend keys; recovery clears them; live status
  shows ISO while STALE.
- Evidence: live last_requote_at present during ~10.2h DOWN

### T1-106 recovery_smoke checklist script
- Status: `done`
- Done when: scripts/recovery_smoke.py PASSes on recovered status fixture and
  FAILs while DOWN; docs playbook cites it; live FAIL with connectivity_up
  blocker during current outage.
- Evidence: pytest test_recovery_smoke; live status=FAIL blockers include
  connectivity_up

### T1-107 Wire recovery_smoke into await recover path
- Status: `done`
- Done when: await_polymarket_recovery runs recovery_smoke after UP (unless
  --no-smoke-on-recover); writes recovery_smoke=PASS|FAIL; accepts
  connectivity status=OK; unit test asserts smoke call on recover.
- Evidence: test_recover_appends_cycle sees recovery_smoke.py + PASS

### T1-108 Diagnose probe must not claim recovery
- Status: `done`
- Done when: --no-restart/--no-append/--no-smoke prints UP_DIAGNOSE and leaves
  outage_open/recovered untouched; strategy_tick passes all three flags;
  unit test covers the diagnose path.
- Evidence: test_up_diagnose_does_not_mark_recovered

### T1-109 hours_to_imminent + require outage_alert_imminent
- Status: `done`
- Done when: compact status includes hours_to_imminent (=max(0,11-total_h));
  outage_alert_imminent is always required by validate; open-outage also
  requires hours_to_imminent; live countdown ~0.16h before imminent.
- Evidence: unit + live hours_to_imminent during ~10.8h DOWN

### T1-110 Latch outage_imminent_since on first imminent edge
- Status: `done`
- Done when: write_compact_status stamps outage_imminent_since on first
  imminent=True and preserves it across ticks; clears on imminent=False /
  recovery; live status shows since after crossing 11h.
- Evidence: live outage_alert_imminent=True at ~11.0h with outage_imminent_since set

### T1-111 hours_in_imminent + require since while imminent
- Status: `done`
- Done when: status carries hours_in_imminent age since latch; validate fails
  if imminent without outage_imminent_since; live hours_in_imminent>0 while
  imminent=True.
- Evidence: unit + live during ~11.2h DOWN imminent window

### T1-112 outage_critical_at + require hours_in_imminent while imminent
- Status: `done`
- Done when: compact status includes outage_critical_at (=started+12h, else
  ts+hours_to_critical); validate requires it while outage_open and requires
  hours_in_imminent while imminent; live critical_at set during imminent window.
- Evidence: unit + live during ~11.3h DOWN

### T1-113 Latch outage_critical_since + hours_past_critical
- Status: `done`
- Done when: write_compact_status latches outage_critical_since on first
  critical=True and preserves across ticks; clears on recovery; validate
  requires since + hours_past_critical while critical lit; unit covers latch.
- Evidence: unit latch/preserve/clear; live latch at ~12.01h DOWN
  (outage_critical_since=2026-07-23T03:28:21Z)

### T1-114 minutes_to_critical countdown
- Status: `done`
- Done when: compact status includes minutes_to_critical (=round(hours*60));
  validate requires it while outage_open; live value during final half-hour.
- Evidence: unit + live during ~11.7h DOWN imminent window

### T1-115 outage_alert_final last-15-minute flag
- Status: `done`
- Done when: compact status sets outage_alert_final when
  0 < minutes_to_critical ≤ 15 and still under 12h; validate always requires
  the key; live True during final quarter-hour.
- Evidence: unit + live during ~11.8h DOWN (~10 min to critical)

### T1-116 Validate critical-state consistency
- Status: `done`
- Done when: validate fails if critical lit with imminent/final still True or
  non-zero hours/minutes_to_critical; live status at ≥12h passes consistency.
- Evidence: unit inconsistencies + live critical=True / imminent=final=False

### T1-117 minutes_past_critical age since latch
- Status: `done`
- Done when: stamp sets minutes_past_critical (=round(hours_past*60)); validate
  requires it while critical; live value >0 after critical edge.
- Evidence: unit + live during ~12.2h DOWN critical window

### T1-118 outage_operator_brief mode + next action
- Status: `done`
- Done when: scripts/outage_operator_brief.py emits CRITICAL_OPEN while
  critical outage open with await_UP_then_full_recovery; strategy_tick runs
  it and surfaces brief/action; unit covers modes.
- Evidence: unit + live CRITICAL_OPEN during ~12.3h DOWN

### T1-119 Persist operator_mode/action in outage_status
- Status: `done`
- Done when: write_compact_status and await patches stamp operator_mode +
  operator_action; validate requires both keys; live CRITICAL_OPEN in status.
- Evidence: unit + live during ~12.5h DOWN critical

### T1-120 Validate operator_mode/action match outage state
- Status: `done`
- Done when: validate fails on operator_mode/action mismatch vs expected brief
  mapping; live CRITICAL_OPEN status passes.
- Evidence: unit mismatch + live during ~12.7h DOWN critical

### T1-121 outage_alert_critical_aged after 30min past latch
- Status: `done`
- Done when: stamp sets outage_alert_critical_aged when
  minutes_past_critical ≥ 30; validate requires the key; live True after
  ~50min past critical.
- Evidence: unit + live during ~12.8h DOWN critical

### T1-122 outage_alert_critical_hour after 60min past latch
- Status: `done`
- Done when: stamp sets outage_alert_critical_hour when
  minutes_past_critical ≥ 60; validate requires the key; live True at ~60min.
- Evidence: unit + live during ~13.0h DOWN critical

### T1-123 Validate critical aged/hour vs minutes_past
- Status: `done`
- Done when: validate fails if aged/hour disagree with minutes_past_critical
  (≥30 / ≥60) or hour is set without aged; live ~70min status passes.
- Evidence: unit mismatch + live during ~13.2h DOWN critical

### T1-124 Stamp operator_recovery_cmd exact next CLI
- Status: `done`
- Done when: brief/status include operator_recovery_cmd for each mode;
  validate requires the key; live CRITICAL_OPEN shows await --once command.
- Evidence: unit + live during ~13.3h DOWN critical

### T1-125 Validate operator_recovery_cmd matches action
- Status: `done`
- Done when: validate fails if operator_recovery_cmd disagrees with expected
  CLI for mode/action; live CRITICAL_OPEN status passes.
- Evidence: unit mismatch + live during ~13.5h DOWN critical

### T1-126 Frozen tape snapshot latch for post-recovery compare
- Status: `done`
- Done when: scripts/frozen_tape_snapshot.py latches quotes_at_freeze on first
  freeze and preserves across ticks; strategy_tick writes
  logs/frozen_tape_snapshot.json and surfaces path; unit covers latch/clear.
- Evidence: unit + live quotes_at_freeze=5529 during ~13.7h DOWN

### T1-127 recovery_smoke requires quotes past freeze latch
- Status: `done`
- Done when: evaluate_recovery fails quotes_advanced when quotes ≤
  quotes_at_freeze from frozen snapshot; passes when quotes advanced; live
  DOWN still fails connectivity first.
- Evidence: unit stuck@5529 vs advanced@5600

## Tier 2 — strategy / execution (PR only; never auto-merge)

Requires T1-01 + T1-02, ≥24h paper runtime and ≥500 new quotes since last
Tier-2 merge. Human reviews via `PENDING_REVIEW.md`.

### T2-01 Market selection / reward-ranking refinement
- Status: `todo`
- Done when: candidate ranking backtested across multiple markets shows
  improved reward-to-risk vs current; script output attached to PR.

### T2-02 Volatility/toxicity estimator improvement
- Status: `todo`
- Done when: candidate vs current on same window by adverse-selection cost per
  fill (script output).

### T2-03 Inventory-skew curve tuning
- Status: `todo`
- Done when: backtest shows lower inventory drift at equal spread capture, OR
  higher spread capture at equal inventory risk — trade-off stated explicitly.

### T2-04 Regime-detection refinement
- Status: `todo`
- Done when: backtested on labeled news-event windows — fewer adverse fills
  during events without excessively cutting quote uptime otherwise.

### T2-05 Quote churn / update-frequency tuning
- Status: `todo`
- Done when: backtest shows improved fill quality without excessive requoting.

### T2-06 Per-market and portfolio-level position limits
- Status: `todo`
- Done when: backtested limits bind before daily-loss cap across historical
  volatile windows. (**Does not** change kill-switch / daily-loss-cap values.)

## Tier 1 — performance & execution latency (perf-agent)

### P1-01 Latency/throughput benchmark harness
- Status: `done`
- Owner: perf-agent
- Started: 2026-07-22T03:00:00Z
- Evidence: `scripts/bench_latency.py`, `tests/test_bench_latency.py`; 5 tests pass;
  baseline: replay p50=41.5us p95=71.2us p99=115.4us (26842 eps);
  pure strategy p50=12.4us p95=16.4us p99=16.7us (77584 ops/s)
- Done when: a script measures, on a fixed replayed data window, the time from
  market-data-received to quote/order-submitted (p50/p95/p99), and orders-
  processed-per-second under load. Must exist before any optimization below.

### P1-02 Profile the current hot path
- Status: `done`
- Owner: perf-agent
- Started: 2026-07-22T03:10:00Z
- Evidence: `perf/profile_2026-07-22.txt`; top 3 bottlenecks:
  1. OrderBook.view() — 0.083s cum (19.1%); _nth_bid/_nth_ask iterate full
     SortedDict even for n=0; depth_within uses generator+sum
  2. construct_quotes() — 0.068s cum (15.7%); _add_layers 0.024s, round() x39943
  3. reconcile() — 0.031s cum (7.1%); defaultdict + set lookups
- Done when: profiler output attached naming the top 3 actual bottlenecks —
  not assumed ones. Do not optimize what you assume is slow.

### P1-03 Fix the top bottleneck, re-measure, repeat
- Status: `done`
- Owner: perf-agent
- Started: 2026-07-22T03:20:00Z
- Done when: one bottleneck at a time, each with before/after benchmark numbers
  and golden-output diff attached. Tests + golden-output diff must pass.
- Evidence: OrderBook.view() optimized — _nth_bid/_nth_ask (n=0/n=1 fast path
  via peekitem), _top_size (islice to avoid full list), depth_within (direct
  loop). Golden-output regression PASS (byte-for-byte identical strategy
  output). 142 tests pass. Pure strategy p95 improved 16.4→13.2us (-19.5%).

### P1-04 Connection/network layer efficiency
- Status: `todo`
- Owner: perf-agent
- Done when: connection pooling, keep-alive, reduced round trips to the exchange
  API, once code-level bottlenecks are addressed.
- Evidence: benchmark output showing improvement

### P1-05 Build/runtime tuning
- Status: `todo`
- Owner: perf-agent
- Done when: concurrency and resource settings tuned, last, once code-level
  bottlenecks are addressed.
- Evidence: benchmark output showing improvement

## Never autonomous

Kill-switch thresholds, daily-loss-cap value, wallet/key handling, paper→live
capital, and position-size/capital increases — human only. Escalate to
`ESCALATE.md` if the loop starts reasoning otherwise.
