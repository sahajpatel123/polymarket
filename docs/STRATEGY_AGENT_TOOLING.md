# Strategy Agent tooling

Operator/agent index for the Tier-1 evidence surface built while paper runtime
accumulates toward the 24h Tier-2 gate. **None of these change pricing math.**

## Every-tick commands

```bash
uv run python scripts/write_weekly_report.py     # overwrite WEEKLY_REPORT.md from live scripts
uv run python scripts/strategy_tick.py           # connectivity + C-01 + summarize + unused-knob scan
uv run python scripts/strategy_tick.py --append --write-weekly
uv run python scripts/append_strategy_cycle.py   # gate+snapshot+rank+health+shadow+c01+outage → JSONL
uv run python scripts/summarize_strategy_cycles.py  # ETA to 24h gate (+ latest shadow AS)
uv run python scripts/paper_health.py            # fail if quotes go stale
uv run python scripts/ensure_paper_collector.py --restart  # relaunch if STALE
uv run python scripts/polymarket_connectivity.py  # REST+WS upstream probe
uv run python scripts/outage_window_report.py     # STALE/DOWN duration from cycles
uv run python scripts/validate_outage_status.py   # schema/freshness check for outage_status.json
uv run python scripts/await_polymarket_recovery.py --once  # check / relaunch+append cycle when UP
uv run python scripts/c01_promotion_checklist.py          # C-01 Tier-2 PR blockers
```

## Evaluation / candidates

| Script | Purpose |
|--------|---------|
| `scripts/compare_strategies.py` | A/B two profiles on one journal (+ holdout) |
| `scripts/sweep_profile_knob.py` | Sweep one StrategyProfile knob |
| `scripts/validate_knob_candidate.py` | Full vs OOS holdout; optional `--also-set` multi-knob |
| `scripts/replay_livecfg.py` | Replay livecfg journal with token auto-detect |
| `scripts/synth_regime_journal.py` | Quiet→jump→recovery synthetic tape (`--dense`) |
| `scripts/rank_vs_realized.py` | Scanner vs realized vs liquidity-oracle ranks |
| `scripts/reward_scorecard.py` | Per-market reward/hour + regime mix |
| `scripts/paper_regime_report.py` | TRENDING mix, false-TRENDING, vol_ratio percentiles + quiet/trend gap |
| `scripts/strategy_snapshot.py` | One-shot gate+metrics+reward+regime+synth A/B |
| `scripts/shadow_adverse_selection.py` | Fill-independent quote-lifetime AS proxy |
| `scripts/candidate_evidence_pack.py` | C-01 validate + counterfactual + shadow (optional `--skip-validate`) |
| `scripts/profile_knob_audit.py` | StrategyProfile fields used vs dead/unused |
| `scripts/quote_churn_report.py` | Quote lifetime + requote-interval percentiles |
| `scripts/verify_metrics_schema.py` | Fail if latest quotes lack required fields |
| `scripts/ensure_paper_collector.py` | Diagnose STALE paper collector; optional `--restart` (refuses while upstream DOWN) |
| `scripts/polymarket_connectivity.py` | REST + market WS upstream probe (outage vs local) |
| `scripts/outage_window_report.py` | STALE/DOWN window durations; optional `--status-out` JSON (incl. `hours_to_tier2_gate`) |
| `scripts/validate_outage_status.py` | Required-key + optional freshness check for `logs/outage_status.json` |
| `scripts/await_polymarket_recovery.py` | Poll until REST+WS UP; collector restart + cycle append; refreshes `logs/outage_status.json` |
| `scripts/write_weekly_report.py` | Overwrite WEEKLY_REPORT.md from gate/metrics/C-01/summarize/deps + outage_status |
| `scripts/unused_knob_toml_scan.py` | Flag unused StrategyProfile knobs still set in TOML (C-04) |
| `scripts/strategy_tick.py` | One-shot tick: connectivity + C-01 + summarize + gate + deps → `outage_status.json` |
| `scripts/c01_promotion_checklist.py` | C-01 Tier-2 PR readiness: READY vs blockers |

## Open candidates

See [STRATEGY_CANDIDATES.md](STRATEGY_CANDIDATES.md). Do not promote Tier-2
pricing/selection changes until `paper_data_gate` reports `tier2_allowed=true`
**and** OOS validation passes on a non-thin holdout.

## Recovery playbook (Polymarket REST/WS DOWN)

1. `uv run python scripts/await_polymarket_recovery.py --once`
   — if UP: restarts collector + appends a cycle (disable with
   `--no-restart-on-recover` / `--no-append-cycle-on-recover`); always
   refreshes `logs/outage_status.json` (disable with `--status-out ''`).
2. Confirm `paper_health` is OK (not STALE) and `tape_frozen=false` on the
   latest cycle line.
3. Re-run `c01_promotion_checklist.py` once quotes advance; do not promote
   C-01 while `outage_alert=true` or holdouts remain thin.

## Hard stops

Kill-switch, daily-loss, wallet/credential logic: human only (`ESCALATE.md`).
