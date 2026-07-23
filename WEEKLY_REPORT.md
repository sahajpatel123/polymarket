# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of 2026-07-23 (UTC)

Generated: `2026-07-23T02:01:11Z` (via `scripts/write_weekly_report.py`)

### System

| Item | Status |
|------|--------|
| Branch | `git log -1` → `7571966 Add recovery_smoke checklist for post-UP verification (T1-106).` |
| Paper trading | `78216 /Users/sahajpatel/Code/polymarket/.venv/bin/python3 /Users/sahajpatel/Code/polymarket/.venv/bin/polymaker run --paper --config-dir livecfg` |
| Loop | 10m Agent-1 strategy-pricing cadence; Tier-2 gated on hours |
| Tier-1 changelog lines | `152` (from `CHANGELOG_AGENT.md`) |
| Tier-1 backlog done | `107` (from `BACKLOG.md` Status: done) |

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| 0 | see `PENDING_REVIEW.md` |

Open candidates: `docs/STRATEGY_CANDIDATES.md` (C-01…C-04).

### Outage / gate snapshot

`logs/outage_status.json`:

```
ts=2026-07-23T02:00:45.794042+00:00
connectivity=status=DOWN rest_ok=False ws_ok=False
outage_open=True
outage_total_h=10.548
outage_alert=True
outage_alert_severe=True
outage_alert_prolonged=True
outage_alert_critical=False
outage_alert_imminent=False
hours_to_critical=1.45
outage_started_at=2026-07-22T15:27:53.062218+00:00
runtime_h=8.37
hours_to_tier2_gate=15.63
quotes=5529
tier2_allowed=False
gate_reason=need_hours>=24.0
runtime_basis=requote
recovered=False
deps_ok=True
deps_bumps=0
deps_flagged=21
tape_frozen=True
eta_paused=True
last_requote_age_s=37682.3
last_quote_age_s=38326.043
last_requote_at=2026-07-22T15:21:56.594663+00:00
last_quote_at=2026-07-22T15:21:56.592663+00:00
health=STALE
ensure_status=NEEDS_RESTART
collector_pid=78216
collector_pids=[78216]
n_cycles=89
c01_status=BLOCKED
c01_blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin
paper_log=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-22
paper_log_files=2
metrics_log=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl
```

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
log_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-22
log_files=2
log_paths=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl,/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-22
metrics_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl
status=OK lines=5950 json_lines=5950 bad_lines=0
runtime_basis=requote
runtime_hours=8.3700
runtime_hours_all_events=19.0176
quote_events=5529
requote_lines=2843
quotes_for_gate=5529
tier2_allowed=false reason=need_hours>=24.0
```

`uv run python scripts/paper_metrics.py`:

```
status=OK quotes=5529 cancels=159 fills=0 marks=19128 realized_spread_usdc=0.000000
```

`uv run python scripts/shadow_adverse_selection.py`:

```
status=OK lifetimes=5529 crossed_frac=0.0000 mean_edge=0.002509 markout_30s=0.000006 n30=446
```

`uv run python scripts/paper_regime_report.py`:

```
status=OK requotes=2843 trending_frac=0.042912 false_trending_frac=1.0 false_trending_attr_frac=1.0 false_trending_cancel_share=0.718563 false_trending_place_share=0.021927 vol_only_frac=1.0 quiet_vol_max=1.989 quiet_vol_p90=1.2196 trend_vol_min=2.029 trend_vol_p50=3.1075 vol_gap=0.04 suggested_vol=2.489 path={'missing_vol': 102, 'vol_only': 20} cancel_per_place=0.030014 transitions=148
```

`uv run python scripts/c01_promotion_checklist.py`:

```
status=BLOCKED blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin runtime_h=8.3700 quotes=5529 health=STALE last_requote_age_s=38356.752 outage_open=True outage_total_h=10.5555 outage_alert=True outage_alert_severe=True outage_alert_prolonged=True outage_alert_critical=False outage_alert_imminent=False oos=False thin=True vol_gap=0.04 quiet_vol_max=1.989 trend_vol_min=2.029 suggested_vol=2.489 suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 false_trending_attr_frac=1.0 boundary_tight=True
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
status=OK cycles=90 runtime_h=8.37 hours_remaining=15.63 eta_wall_h=None eta_paused=True outage_open=True outage_total_h=10.548 outage_alert=True outage_alert_severe=True outage_alert_prolonged=True outage_alert_critical=False outage_alert_imminent=False hours_to_tier2_gate=15.63 hours_to_critical=1.45 outage_started_at=2026-07-22T15:27:53.062218+00:00 tier2_allowed=False quotes_per_wall_h=245.13 health=STALE last_requote_age_s=38327.791 tape_frozen=True connectivity=DOWN crossed_frac=0.0000 markout_30s=0.000006 false_trending_frac=0.0 false_trending_cancel_share=0.0 vol_only_frac=None vol_gap=None quiet_vol_max=None trend_vol_min=None suggested_vol=None false_trending_attr_frac=None c01=BLOCKED c01_blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 unused_set=9 paper_schema=OK
```

### Dependency / security audit

`uv run python scripts/deps_audit.py`:

```
status=OK packages=83 flagged=21 bumps=0
ok=True
```

### Credentials / certificates

No expiry tracker in-repo. `.env` is gitignored; operator must rotate
`PK` / builder creds outside this loop.

### Blockers (informational)

- Parse C-01 / summarize / outage_status above for outage_alert, tape_frozen,
  ETA pause, tier2_allowed, and promotion blockers. Do not promote Tier-2
  while health is STALE or holdouts are thin.
- Live capital / size increases remain human-only (`ESCALATE.md`).
