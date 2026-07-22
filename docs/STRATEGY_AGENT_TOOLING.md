# Strategy Agent tooling

Operator/agent index for the Tier-1 evidence surface built while paper runtime
accumulates toward the 24h Tier-2 gate. **None of these change pricing math.**

## Every-tick commands

```bash
uv run python scripts/append_strategy_cycle.py   # gate+snapshot+rank+health+shadow → JSONL
uv run python scripts/summarize_strategy_cycles.py  # ETA to 24h gate (+ latest shadow AS)
uv run python scripts/paper_health.py            # fail if quotes go stale
uv run python scripts/ensure_paper_collector.py --restart  # relaunch if STALE
uv run python scripts/polymarket_connectivity.py  # REST+WS upstream probe
uv run python scripts/outage_window_report.py     # STALE/DOWN duration from cycles
uv run python scripts/await_polymarket_recovery.py --once  # check / relaunch when UP
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
| `scripts/ensure_paper_collector.py` | Diagnose STALE paper collector; optional `--restart` |
| `scripts/polymarket_connectivity.py` | REST + market WS upstream probe (outage vs local) |
| `scripts/outage_window_report.py` | STALE/DOWN window durations from strategy_cycles |
| `scripts/await_polymarket_recovery.py` | Poll until REST+WS UP; optional collector restart |
| `scripts/c01_promotion_checklist.py` | C-01 Tier-2 PR readiness: READY vs blockers |

## Open candidates

See [STRATEGY_CANDIDATES.md](STRATEGY_CANDIDATES.md). Do not promote Tier-2
pricing/selection changes until `paper_data_gate` reports `tier2_allowed=true`
**and** OOS validation passes on a non-thin holdout.

## Hard stops

Kill-switch, daily-loss, wallet/credential logic: human only (`ESCALATE.md`).
