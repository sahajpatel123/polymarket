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
uv run python scripts/outage_operator_brief.py    # one-line mode + next recovery action
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
| `scripts/outage_operator_brief.py` | One-line mode (`CRITICAL_OPEN` / …) + next recovery action + exact CLI (T1-118/T1-124); also stamped into status as `operator_mode` / `operator_action` / `operator_recovery_cmd` (T1-119/T1-124) |
| `scripts/await_polymarket_recovery.py` | Poll until REST+WS UP; collector restart + cycle append; refreshes `logs/outage_status.json` |
| `scripts/write_weekly_report.py` | Overwrite WEEKLY_REPORT.md from gate/metrics/C-01/summarize/deps + outage_status |
| `scripts/unused_knob_toml_scan.py` | Flag unused StrategyProfile knobs still set in TOML (C-04) |
| `scripts/strategy_tick.py` | One-shot tick: connectivity + C-01 + summarize + gate + deps → `outage_status.json` |
| `scripts/c01_promotion_checklist.py` | C-01 Tier-2 PR readiness: READY vs blockers |

## `logs/outage_status.json` field contract

Compact monitor snapshot written by `strategy_tick` / `outage_window_report`
/ `await_polymarket_recovery`. Validated by `validate_outage_status.py`.

**Required** (fail if missing): `ts`, `outage_open`, `outage_total_h`,
`outage_alert` (≥3h), `outage_alert_severe` (≥5h), `outage_alert_prolonged`
(≥8h), `outage_alert_critical` (≥12h), `outage_alert_imminent` (11–12h window),
`outage_alert_final` (last 15 min before critical), `outage_alert_critical_aged`
(≥30 min past critical latch), `outage_alert_critical_hour` (≥60 min past
latch), `operator_mode`, `operator_action`, `operator_recovery_cmd` (exact next
CLI), `hours_to_tier2_gate`,
`runtime_h`, `quotes` (int from live gate when available).

`outage_alert_imminent` is set when `1h >= hours_to_critical > 0` (i.e. outage
age in [11h, 12h)) — final-hour warning before critical (T1-104).
`outage_alert_final` is set when `0 < minutes_to_critical <= 15` and still under
12h — last-quarter-hour warning (T1-115).
`hours_to_imminent` counts down to that 11h threshold (T1-109). When imminent
first trips, `outage_imminent_since` latches the UTC ISO timestamp and stays
fixed until recovery clears it (T1-110). `hours_in_imminent` is age since that
latch; while imminent is True, validate requires `outage_imminent_since` and
`hours_in_imminent` (T1-111/T1-112). `outage_critical_at` is the UTC ISO when
the 12h critical alert will trip (`outage_started_at + 12h`, with fallback to
`ts + hours_to_critical`) and is required while `outage_open` (T1-112).
`minutes_to_critical` is the rounded whole-minute countdown from
`hours_to_critical` and is also required while open (T1-114). When
`outage_alert_critical` first trips, `outage_critical_since` latches and
`hours_past_critical` / `minutes_past_critical` age from that edge; validate
requires all three while critical is lit (T1-113/T1-117).
`outage_alert_critical_aged` turns True once `minutes_past_critical >= 30`
(T1-121). `outage_alert_critical_hour` turns True once
`minutes_past_critical >= 60` (T1-122).

After each `strategy_tick`, `quotes` / `runtime_h` / `hours_to_tier2_gate` are
refreshed from the live `paper_data_gate` (T1-99), not only the cycle trail.
The tick status line reads the merged `outage_status.json` so operators see
int quotes and `hours_to_critical` (T1-100). `strategy_tick` refreshes outage
status **before** `summarize_strategy_cycles`, which overlays live
`hours_to_critical` / `outage_started_at` / `outage_critical_at` (T1-102/T1-112).

**Recommended** (warned if missing): `connectivity`, `tier2_allowed`,
`gate_reason`, `runtime_basis`, `tape_frozen`, `eta_paused`,
`last_requote_age_s`, `last_requote_at`, `health`, `ensure_status`,
`collector_pid`, `deps_ok`, `n_cycles`, `c01_status`, `c01_blockers`,
`paper_log`, `paper_log_files`, `metrics_log`, `recovery_smoke`,
`recovery_smoke_blockers`.

While `outage_open=true`, validate also **requires** `hours_to_critical`,
`minutes_to_critical`, `hours_to_imminent`, `outage_started_at`, and
`outage_critical_at` (T1-103/T1-109/T1-112/T1-114) — empty/null counts as
missing. While `outage_alert_imminent=true`, validate also **requires**
`outage_imminent_since` and `hours_in_imminent` (T1-111/T1-112). While
`outage_alert_critical=true`, validate also **requires**
`outage_critical_since`, `hours_past_critical`, and `minutes_past_critical`
(T1-113/T1-117). While critical is
lit, validate also fails on inconsistencies: imminent/final still True, or
non-zero `hours_to_critical` / `minutes_to_critical` (T1-116).
`operator_mode` / `operator_action` must match the outage state
(CRITICAL_OPEN while critical+open, etc.) (T1-120).
`outage_alert_critical_aged` / `outage_alert_critical_hour` must agree with
`minutes_past_critical` thresholds (30 / 60) (T1-123).

`last_requote_at` / `last_quote_at` are UTC ISO timestamps derived from live
paper_health ages (T1-105).

**Paper log rotation:** `TimedRotatingFileHandler` rolls
`livecfg/logs/paper.jsonl` to `paper.jsonl.YYYY-MM-DD` at midnight. Richest-log
discovery (T1-96) includes those dated archives so the gate does not collapse
onto the new empty file. Gate runtime / paper_health freshness **union** the
active file with same-dir rotations (T1-98) so post-recovery quoting into the
new `paper.jsonl` continues the 24h clock instead of freezing on the archive.
`paper_log` is still the richest single member; `paper_log_files>1` means a
rotation split is in play.

During a Polymarket outage expect `outage_open=true`, `health=STALE`,
`ensure_status=NEEDS_RESTART` (collector alive; do **not** force-restart while
upstream is DOWN), and frozen `quotes` / `runtime_h`.

## Open candidates

See [STRATEGY_CANDIDATES.md](STRATEGY_CANDIDATES.md). Do not promote Tier-2
pricing/selection changes until `paper_data_gate` reports `tier2_allowed=true`
**and** OOS validation passes on a non-thin holdout.

## Recovery playbook (Polymarket REST/WS DOWN)

1. `uv run python scripts/await_polymarket_recovery.py --once`
   — if UP: restarts collector + appends a cycle + runs `recovery_smoke.py`
   (disable with `--no-restart-on-recover` / `--no-append-cycle-on-recover` /
   `--no-smoke-on-recover`); always refreshes `logs/outage_status.json`
   (disable with `--status-out ''`). On success writes
   `recovery_smoke=PASS|FAIL` into the status file (T1-107).
   Diagnose-only probes (`--no-restart-on-recover --no-append-cycle-on-recover
   --no-smoke-on-recover`, as used by `strategy_tick`) print `UP_DIAGNOSE` and
   **do not** clear `outage_open` / claim `recovered` (T1-108).
2. `uv run python scripts/recovery_smoke.py --min-quotes 5529`
   — PASS only when connectivity OK (REST+WS), outage closed, health OK, tape
   unfrozen, requote runtime basis, paper-log family present, and outage alerts
   cleared (T1-106). Use `--allow-stale-health` only for diagnose-during-DOWN.
3. Confirm `paper_health` is OK (not STALE) and `tape_frozen=false` on the
   latest cycle line.
4. Re-run `c01_promotion_checklist.py` once quotes advance; do not promote
   C-01 while `outage_alert=true` or holdouts remain thin.

## Hard stops

Kill-switch, daily-loss, wallet/credential logic: human only (`ESCALATE.md`).
