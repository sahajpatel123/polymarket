# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of 2026-07-22 (UTC)

Generated: `2026-07-22T23:48:52Z` (via `scripts/write_weekly_report.py`)

### System

| Item | Status |
|------|--------|
| Branch | `git log -1` → `09a1b8d Require outage_alert_prolonged in outage_status validation (T1-93).` |
| Paper trading | `78216 /Users/sahajpatel/Code/polymarket/.venv/bin/python3 /Users/sahajpatel/Code/polymarket/.venv/bin/polymaker run --paper --config-dir livecfg` |
| Loop | 10m Agent-1 strategy-pricing cadence; Tier-2 gated on hours |
| Tier-1 changelog lines | `139` (from `CHANGELOG_AGENT.md`) |
| Tier-1 backlog done | `94` (from `BACKLOG.md` Status: done) |

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| 0 | see `PENDING_REVIEW.md` |

Open candidates: `docs/STRATEGY_CANDIDATES.md` (C-01…C-04).

### Outage / gate snapshot

`logs/outage_status.json`:

```
ts=2026-07-22T23:48:52.623354+00:00
connectivity=status=DOWN rest_ok=False ws_ok=False
outage_open=True
outage_total_h=8.3498
outage_alert=True
outage_alert_severe=True
outage_alert_prolonged=True
runtime_h=8.37
hours_to_tier2_gate=15.63
quotes=5529.0
tier2_allowed=False
gate_reason=need_hours>=24.0
runtime_basis=requote
recovered=False
deps_ok=True
deps_bumps=0
deps_flagged=21
tape_frozen=True
eta_paused=True
last_requote_age_s=30415.953
last_quote_age_s=30415.955
health=STALE
ensure_status=NEEDS_RESTART
collector_pid=78216
collector_pids=[78216]
n_cycles=79
```

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
log_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl
metrics_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl
status=OK lines=5551 json_lines=5551 bad_lines=0
runtime_basis=requote
runtime_hours=8.3700
runtime_hours_all_events=16.8173
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
status=BLOCKED blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin runtime_h=8.3700 quotes=5529 health=STALE last_requote_age_s=30418.195 outage_open=True outage_total_h=8.3504 outage_alert=True outage_alert_severe=True outage_alert_prolonged=True oos=False thin=True vol_gap=0.04 quiet_vol_max=1.989 trend_vol_min=2.029 suggested_vol=2.489 suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 false_trending_attr_frac=1.0 boundary_tight=True
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
status=OK cycles=79 runtime_h=8.37 hours_remaining=15.63 eta_wall_h=None eta_paused=True outage_open=True outage_total_h=8.3504 outage_alert=True outage_alert_severe=True outage_alert_prolonged=True hours_to_tier2_gate=15.63 tier2_allowed=False quotes_per_wall_h=285.24 health=STALE last_requote_age_s=29810.399 tape_frozen=True connectivity=SKIPPED crossed_frac=0.0000 markout_30s=0.000006 false_trending_frac=1.0 false_trending_cancel_share=0.718563 vol_only_frac=1.0 vol_gap=0.04 quiet_vol_max=1.989 trend_vol_min=2.029 suggested_vol=2.489 false_trending_attr_frac=1.0 c01=BLOCKED c01_blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 unused_set=9 paper_schema=OK
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
