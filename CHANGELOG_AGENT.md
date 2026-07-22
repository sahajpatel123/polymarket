# CHANGELOG_AGENT

Append-only record of autonomous loop cycles. One line per cycle.

Format: `ISO8601 | Tier | description | evidence | outcome`

---

2026-07-21T20:57:00Z | Tier1 | Bootstrap agent docs/protocol; fix missing pytest-asyncio via uv sync --extra dev; no paper data (Tier2 gated) | `uv run pytest` → 111 passed, 2 skipped; paper.jsonl absent | merged
2026-07-21T21:10:00Z | Tier1 | T1-01 paper metrics logger + analyze script; wire engine quote/cancel/fill/mark; backlog+long-run protocol | `uv run pytest` + `tests/test_metrics.py`; `scripts/paper_metrics.py` on fixture | merged
2026-07-21T21:22:00Z | Tier1 | T1-02 deterministic journal replay harness → metrics JSONL analyzable by T1-01 | `uv run pytest tests/test_replay.py`; `scripts/replay_backtest.py` → n_quote=8 n_mark=5 | merged
2026-07-21T21:38:00Z | Tier1 | T1-03 alerting wrapper: five required kinds + verify script (thresholds untouched) | `scripts/verify_alerts.py` all_required_posted=true; pytest 125 passed | merged
2026-07-21T21:52:00Z | Tier1 | T1-04 structured JSON logs with daily rotation + market/time grep | `tests/test_logging.py`; grep_logs matches; pytest 127 passed | merged
2026-07-21T22:06:00Z | Tier1 | T1-05 edge-case matrix + config/quoting/reconcile tests | docs/EDGE_CASES.md; pytest 137 passed | merged
2026-07-21T22:20:00Z | Tier1 | T1-06 GitHub Actions CI runs full pytest on push/PR to main | .github/workflows/ci.yml; local pytest 137 passed | merged
2026-07-21T22:36:00Z | Tier1 | T1-07 deps audit vs uv.lock hashes + baseline drift + METADATA hints | scripts/deps_audit.py ok=true; tests/test_deps_audit.py | merged
2026-07-21T22:51:00Z | Tier1 | T1-08 local HTML metrics dashboard from T1-01 log | scripts/metrics_dashboard.py; pytest 142 passed | merged
2026-07-21T23:06:00Z | Tier1 | Weekly status report; Tier2 skipped (no paper log) | paper_data_gate NO_LOG; deps_audit ok=true; WEEKLY_REPORT.md | merged
2026-07-21T23:20:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-21T23:35:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-21T23:50:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T00:05:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T00:20:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T00:35:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T00:50:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T01:05:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T01:20:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T01:35:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T01:50:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T02:05:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T02:20:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T02:35:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T02:50:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T03:05:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T03:20:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T03:35:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T03:50:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T04:05:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T04:20:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T04:35:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T04:50:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T05:05:00Z | Tier1 | skipped — no new data (Tier1 complete; Tier2 gated NO_LOG)
2026-07-22T06:30:00Z | Tier1 | skipped — no new data (catch-up after tick gap; Tier2 gated NO_LOG)
2026-07-22T06:50:00Z | perf-agent | P1-01 latency/throughput benchmark harness | scripts/bench_latency.py + tests/test_bench_latency.py (5 tests); baseline: replay p50=41.5us p95=71.2us p99=115.4us (26842 eps); pure p50=12.4us p95=16.4us p99=16.7us (77584 ops/s) | merged
2026-07-22T07:00:00Z | perf-agent | P1-02 profile hot path (cProfile) | perf/profile_2026-07-22.txt; top 3: OrderBook.view() 19.1%, construct_quotes() 15.7%, reconcile() 7.1% | merged
2026-07-22T07:15:00Z | perf-agent | P1-03 optimize OrderBook.view() | _nth_bid/_nth_ask fast path (peekitem n=0/n=1), _top_size (islice), depth_within (direct loop); golden-output PASS (byte-identical); 142 tests pass; pure p95 16.4→13.2us (-19.5%) | merged
2026-07-22T07:20:00Z | Tier1 | T1-09 strategy A/B compare harness + holdout slice (Tier2 gated NO_LOG) | `uv run pytest` → 140 passed, 2 skipped; `compare_strategies.py` status=OK window=full dn_quote=0.0 | merged
2026-07-22T07:01:00Z | Tier1 | T1-10 synth regime journal + named-profile compare; paper gate finds livecfg logs; paper collector started | pytest 147 passed, 2 skipped; compare newsom-mm vs romania-pm dn_quote=-4.0; paper_data_gate status=OK requote_lines growing | merged

2026-07-22T07:08:00Z | Tier1 | T1-11 strategy_snapshot + paper_metrics livecfg path; paper collecting (Tier2 gated) | strategy_snapshot status=OK; pytest green; reward_accrual on live paper | merged

2026-07-22T07:18:00Z | Tier1 | T1-12 paper regime/churn report; paper ~18m (Tier2 gated) | paper_regime_report status=OK requotes=104 trending_frac=0.067 cancel_per_place=0.05 transitions=8 trending_flowz_mean=0.0; pytest test_paper_regime_report PASS | merged

2026-07-22T07:28:00Z | Tier1 | T1-13 knob sweep harness; synth micro-jitter; paper ~28m (Tier2 gated) | sweep reprice_ticks 1:+3q/c 5:-1q/c; pytest sweep+compare 6 passed; snapshot quotes=298 reward~10.09 | merged

2026-07-22T07:38:00Z | Tier1 | T1-14 replay_livecfg token auto-detect + live-tape A/B; paper ~38m (Tier2 gated) | replay_livecfg status=OK markets=2 replay_quotes=23; live-tiny vs newsom-mm dn_quote=+27/+22; pytest test_replay_livecfg PASS | merged

2026-07-22T07:49:00Z | Tier1 | T1-15 paper_data_gate counts metrics quotes; paper ~0.82h (Tier2 gated on hours only) | gate quote_events=531 quotes_for_gate=531 reason=need_hours>=24.0; test_paper_data_gate PASS; reward_accrual_sum~17.5 | merged

2026-07-22T07:58:00Z | Tier1 | T1-16 reward scorecard; live trend_vol_ratio sweep evidence (Tier2 gated on hours) | scorecard status=OK reward_sum~21.3 top_rph~12.8; sweep trend_vol_ratio 8:dn_quote=-24; pytest test_reward_scorecard PASS; gate quotes=627 need_hours>=24 | merged

2026-07-22T08:08:00Z | Tier1 | T1-17 OOS knob validator; trend_vol_ratio candidate NOT replicated on thin holdout (Tier2 gated) | validate status=OK full_dn_quote=-24 holdout_dn_quote=0 oos_replicated=false thin_holdout=true; docs/STRATEGY_CANDIDATES.md; gate quotes=737 need_hours>=24 | merged

2026-07-22T08:18:00Z | Tier1 | T1-18 event holdout + market token filter; C-01 still fails OOS (Tier2 gated) | validate events split full_dn_quote=-27 holdout_dn_quote=0 oos_replicated=false; pytest compare+validate 9 passed; gate quotes=845 need_hours>=24 | merged

2026-07-22T08:28:00Z | Tier1 | T1-19 snapshot adds reward+regime; C-01 still watching (Tier2 gated on hours) | strategy_snapshot status=OK; TRENDING at t+84m with flowz~0; gate quotes=951 need_hours>=24 | merged

2026-07-22T08:29:00Z | Tier1 | T1-19 follow-up: land strategy_snapshot reward+regime code | snapshot status includes top_reward_per_hour+trending_frac | merged

2026-07-22T08:38:00Z | Tier1 | T1-20 scanner vs realized reward ranks; inverted on live pair (Tier2 gated) | rank_vs_realized status=OK spearman=-1.0 disagreements=2; pytest 3 passed; gate quotes=1069 need_hours>=24 | merged

2026-07-22T08:48:00Z | Tier1 | T1-20b reward decomposition: in-band~100o rank invert = daily_rate gap (Tier2 gated) | rank_vs_realized daily 308 vs 214, in_band_frac=1.0 both; spearman=-1.0; gate quotes=1175 need_hours>=24 | merged

2026-07-22T08:58:00Z | Tier1 | T1-22 scanner components: rebate_pot drives Newsom rank; paper realized is liquidity-reward only (Tier2 gated) | rebate 93 vs 10; daily_rate 214 vs 308; in_band_frac=1; spearman=-1; gate quotes=1281 need_hours>=24 | merged

2026-07-22T09:08:00Z | Tier1 | T1-23 cycle history appender; C-01 still fails OOS at 2.1h (Tier2 gated) | append_strategy_cycle status=OK; validate full_dn_quote=-42 holdout=0 oos=false; gate quotes=1384 need_hours>=24 | merged

2026-07-22T09:18:00Z | Tier1 | T1-24 liquidity-oracle rank; scanner vs oracle spearman=-1 under zero-fill paper (Tier2 gated) | rank_vs_realized spearman_vs_oracle=-1.0; pytest pass; gate quotes=1496 need_hours>=24 | merged

2026-07-22T09:28:00Z | Tier1 | T1-25 paper_health staleness watchdog; collector fresh (Tier2 gated) | paper_health status=OK; append cycle includes health; pytest test_paper_health PASS; gate quotes=1611 need_hours>=24 | merged

2026-07-22T09:38:00Z | Tier1 | T1-26 cycle summary ETA to 24h gate; paper healthy (Tier2 gated) | summarize_strategy_cycles status=OK; append health=OK; gate quotes=1725 need_hours>=24 | merged

2026-07-22T09:48:00Z | Tier1 | T1-27 strategy agent tooling index; paper ~2.8h healthy (Tier2 gated) | docs/STRATEGY_AGENT_TOOLING.md; summarize eta_wall_h~21.1; health=OK quotes=1839 | merged

2026-07-22T09:58:00Z | Tier1 | skipped — no new Tier1; paper collecting toward 24h (Tier2 gated) | append health=OK runtime_h=2.97 quotes=1950; summarize hours_remaining=21.03 eta_wall_h~20.9; spearman=-1.0 | skipped

2026-07-22T10:08:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | runtime_h=3.14 quotes=2067 health=OK hours_remaining=20.86 eta_wall_h~20.9 spearman=-1.0 | skipped

2026-07-22T10:18:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | runtime_h=3.31 quotes=2173 health=OK hours_remaining=20.69 eta_wall_h~20.6 | skipped

2026-07-22T10:28:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | runtime_h=3.4726 quotes=2285 health=OK | skipped

2026-07-22T10:38:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=12 runtime_h=3.6404 hours_remaining=20.3596 eta_wall_h=20.3035 quotes_per_wall_h=672.58 health=OK | skipped

2026-07-22T10:48:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=13 runtime_h=3.8075 hours_remaining=20.1925 eta_wall_h=20.1317 quotes_per_wall_h=676.32 health=OK | skipped

2026-07-22T10:58:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=14 runtime_h=3.9726 hours_remaining=20.0274 eta_wall_h=19.9819 quotes_per_wall_h=676.74 health=OK | skipped

2026-07-22T11:08:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=15 runtime_h=4.1392 hours_remaining=19.8608 eta_wall_h=19.814 quotes_per_wall_h=677.05 health=OK | skipped

2026-07-22T11:18:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=16 runtime_h=4.3064 hours_remaining=19.6936 eta_wall_h=19.6543 quotes_per_wall_h=680.09 health=OK | skipped

2026-07-22T11:28:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=17 runtime_h=4.4732 hours_remaining=19.5268 eta_wall_h=19.4872 quotes_per_wall_h=680.02 health=OK | skipped

2026-07-22T11:38:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=18 runtime_h=4.6395 hours_remaining=19.3605 eta_wall_h=19.3242 quotes_per_wall_h=674.75 health=OK | skipped

2026-07-22T11:48:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=19 runtime_h=4.8048 hours_remaining=19.1952 eta_wall_h=19.1714 quotes_per_wall_h=673.82 health=OK | skipped

2026-07-22T11:58:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=20 runtime_h=4.9722 hours_remaining=19.0278 eta_wall_h=18.9956 quotes_per_wall_h=672.47 health=OK | skipped

2026-07-22T12:08:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=21 runtime_h=5.1369 hours_remaining=18.8631 eta_wall_h=18.8427 quotes_per_wall_h=671.87 health=OK | skipped

2026-07-22T12:18:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=22 runtime_h=5.3064 hours_remaining=18.6936 eta_wall_h=18.671 quotes_per_wall_h=672.67 health=OK | skipped

2026-07-22T12:28:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=23 runtime_h=5.469 hours_remaining=18.531 eta_wall_h=18.5204 quotes_per_wall_h=670.06 health=OK | skipped

2026-07-22T12:38:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=24 runtime_h=5.6346 hours_remaining=18.3654 eta_wall_h=18.3635 quotes_per_wall_h=668.91 health=OK | skipped

2026-07-22T12:48:00Z | Tier1 | skipped — waiting on 24h paper gate (collector healthy) | status=OK cycles=25 runtime_h=5.8056 hours_remaining=18.1944 eta_wall_h=18.17 quotes_per_wall_h=670.46 health=OK | skipped

2026-07-22T12:55:00Z | Tier1 | T1-28 shadow adverse-selection + T1-29 evidence pack; C-01 still fails OOS on denser tape | pytest 163 passed; shadow lifetimes~3960 crossed_frac=0 markout_30s~0; C-01 full_dn_quote=-93/-14 oos=false thin_holdout | merged

2026-07-22T13:02:00Z | Tier1 | T1-30 cycle history wires shadow AS; C-03 multi-knob null screen on ~6h tape | pytest summarize+shadow pass; trend_flow_z in-sample only (oos=false); reprice/gamma/event_jump/c_tox dn_quote=0 | merged

2026-07-22T13:10:00Z | Tier1 | T1-31 StrategyProfile knob audit; event_sweep_levels newly confirmed unused | pytest pass; status n_used=26 n_unused=3 unused=end_date_taper_days,event_sweep_levels,exit_urgency_s | merged

2026-07-22T13:20:00Z | Tier1 | T1-32 richest paper-log discovery — fix gate shadowing by tiny logs/paper.jsonl | gate restored livecfg runtime_h=6.33 quotes=4200; pytest test_paper_data_gate pass | merged

2026-07-22T13:31:00Z | Tier1 | T1-33 richest-log discovery across health/regime/scorecard/rank/snapshot; weekly report refresh | gate livecfg 6.51h/4318q; WEEKLY_REPORT overwritten; pytest related pass | merged

2026-07-22T13:40:00Z | Tier1 | T1-34 quote lifetime/requote churn report (T2-05 evidence) | pytest pass; live life_p50/rq_p50 on ~6.6h tape; wired into cycle append | merged

2026-07-22T13:50:00Z | Tier1 | T1-35 quote metrics emit fv_yes; shadow AS prefers it over mark heuristic | pytest shadow+metrics+replay 10 passed; paper ~6.8h/4500q collecting | merged

2026-07-22T14:02:00Z | Tier1 | T1-36 flush quote metrics immediately + schema verifier; restarted paper collector | pytest metrics+schema pass; live quotes now have fv_yes; gate ~7.0h/4626q | merged

2026-07-22T14:10:00Z | Tier1 | T1-37 schema in cycle history; C-01 recheck at ~7.1h still fails OOS; document C-04 unused knobs | append schema=OK; evidence pack full_dn=-111/-14 oos=false thin_holdout | merged

2026-07-22T14:20:00Z | Tier1 | T1-38 dense multi-cycle synth; offline C-01 clears thin_holdout but still fails OOS | regime_dense 712 events; validate thin_holdout=false oos=false; pytest synth_dense pass | merged

2026-07-22T14:30:00Z | Tier1 | T1-39 false_trending_frac in regime report — live frac=1.0 (all TRENDING are low flow_z) | pytest pass; C-01 smoking gun updated; still no Tier2 merge | merged

2026-07-22T14:40:00Z | Tier1 | T1-40 false_trending cancel/place share + cycle wiring — live cancel_share≈0.72 | pytest pass; C-01 refreshed thin OOS; no Tier2 | merged

2026-07-22T14:50:00Z | Tier1 | T1-41 requote vol_ratio + dual-path TRENDING attribution (flow/vol/both) | pytest pass; restart paper collector for new field; no Tier2 | merged

2026-07-22T15:00:00Z | Tier1 | T1-42 paper requote schema verifier + vol_only_frac in cycles; old collector SIGTERM expected after T1-41 restart | pytest pass; paper_schema catching_up/OK; no Tier2 | merged

2026-07-22T15:10:00Z | Tier1 | T1-43 validate --also-set multi-override; C-01 vol8+flow2 ≡ vol8 on vol_only tape | pytest pass; still thin OOS; no Tier2 | merged

2026-07-22T15:20:00Z | Tier1 | T1-44 freeze journal in evidence pack + holdout_base_nq; reject live-append OOS false positive | pytest pass; frozen pack still thin OOS; no Tier2 | merged

2026-07-22T15:35:00Z | Tier1 | T1-45 ensure_paper_collector restart helper; live still STALE — Polymarket market WS handshake timeouts | pytest pass; collector restarted pid recorded; gate clock pauses until WS recovers | merged

2026-07-22T15:40:00Z | Tier1 | T1-46 polymarket_connectivity probe + eta_paused when health STALE; live REST+WS DOWN | pytest pass; collector left running to reconnect; no Tier2 | merged

2026-07-22T15:50:00Z | Tier1 | T1-47 outage_window_report + summarize outage_open; Polymarket still DOWN / paper STALE | pytest pass; quotes frozen ~5529; no Tier2 | merged

2026-07-22T16:00:00Z | Tier1 | T1-48 offline TRENDING counterfactual for C-01; suppress_frac=1.0 at vol=8 on attributed rows; Polymarket still DOWN | pytest pass; no Tier2 | merged

2026-07-22T16:10:00Z | Tier1 | T1-49 counterfactual --sweep-vol/--by-market; Newsom 3/5/8=0.44/0.81/1.0 Vance 0.25/0.50/1.0; Polymarket still DOWN | pytest pass; no Tier2 | merged

2026-07-22T16:20:00Z | Tier1 | T1-50 evidence pack embeds C-01 counterfactual + --skip-validate; both markets suppress=1.0 at vol=8; Polymarket still DOWN | pack OK; no Tier2 | merged

2026-07-22T16:30:00Z | Tier1 | T1-51 await_polymarket_recovery + append --skip-connectivity/--with-counterfactual; outage >1h still DOWN | pytest pass; no Tier2 | merged

2026-07-22T16:40:00Z | Tier1 | T1-52 paper_data_gate runtime from requote span only — stop outage noise padding toward 24h; live 8.37h vs all_events 9.66h | pytest pass; Polymarket still DOWN; no Tier2 | merged

2026-07-22T16:50:00Z | Tier1 | T1-53 paper_log_score ranks by requote runtime (align with gate); ignore outage padding when picking richest log | pytest pass; Polymarket still DOWN; no Tier2 | merged
