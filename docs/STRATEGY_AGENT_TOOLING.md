# Strategy Agent tooling

Operator/agent index for the Tier-1 evidence surface built while paper runtime
accumulates toward the 24h Tier-2 gate. **None of these change pricing math.**

## Every-tick commands

```bash
uv run python scripts/append_strategy_cycle.py   # gate+snapshot+rank+health → JSONL
uv run python scripts/summarize_strategy_cycles.py  # ETA to 24h gate
uv run python scripts/paper_health.py            # fail if quotes go stale
```

## Evaluation / candidates

| Script | Purpose |
|--------|---------|
| `scripts/compare_strategies.py` | A/B two profiles on one journal (+ holdout) |
| `scripts/sweep_profile_knob.py` | Sweep one StrategyProfile knob |
| `scripts/validate_knob_candidate.py` | Full vs OOS holdout; flags non-replication |
| `scripts/replay_livecfg.py` | Replay livecfg journal with token auto-detect |
| `scripts/synth_regime_journal.py` | Quiet→jump→recovery synthetic tape |
| `scripts/rank_vs_realized.py` | Scanner vs realized vs liquidity-oracle ranks |
| `scripts/reward_scorecard.py` | Per-market reward/hour + regime mix |
| `scripts/paper_regime_report.py` | TRENDING/QUIET mix and cancel churn |
| `scripts/strategy_snapshot.py` | One-shot gate+metrics+reward+regime+synth A/B |

## Open candidates

See [STRATEGY_CANDIDATES.md](STRATEGY_CANDIDATES.md). Do not promote Tier-2
pricing/selection changes until `paper_data_gate` reports `tier2_allowed=true`
**and** OOS validation passes on a non-thin holdout.

## Hard stops

Kill-switch, daily-loss, wallet/credential logic: human only (`ESCALATE.md`).
