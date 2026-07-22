# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of 2026-07-22 (UTC)

Generated: `2026-07-22T20:28:48Z` (via `scripts/write_weekly_report.py`)

### System

| Item | Status |
|------|--------|
| Branch | `git log -1` → `a2e2d0a Refresh WEEKLY_REPORT for the extended Polymarket outage (T1-73).` |
| Paper trading | `78216 /Users/sahajpatel/Code/polymarket/.venv/bin/python3 /Users/sahajpatel/Code/polymarket/.venv/bin/polymaker run --paper --config-dir livecfg` |
| Loop | 10m Agent-1 strategy-pricing cadence; Tier-2 gated on hours |

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| 0 | see `PENDING_REVIEW.md` |

Open candidates: `docs/STRATEGY_CANDIDATES.md` (C-01…C-04).

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
log_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl
metrics_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl
status=OK lines=4946 json_lines=4946 bad_lines=0
runtime_basis=requote
runtime_hours=8.3700
runtime_hours_all_events=13.4834
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
status=BLOCKED blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin runtime_h=8.3700 quotes=5529 health=STALE last_requote_age_s=18414.199 outage_open=True outage_total_h=5.0159 outage_alert=True oos=False thin=True vol_gap=0.04 quiet_vol_max=1.989 trend_vol_min=2.029 suggested_vol=2.489 suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 false_trending_attr_frac=1.0 boundary_tight=True
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
status=OK cycles=69 runtime_h=8.37 hours_remaining=15.63 eta_wall_h=None eta_paused=True outage_open=True outage_total_h=5.0159 quotes_per_wall_h=365.15 health=STALE last_requote_age_s=18383.25 tape_frozen=True connectivity=SKIPPED crossed_frac=0.0000 markout_30s=0.000006 false_trending_frac=1.0 false_trending_cancel_share=0.718563 vol_only_frac=1.0 vol_gap=0.04 quiet_vol_max=1.989 trend_vol_min=2.029 suggested_vol=2.489 false_trending_attr_frac=1.0 c01=BLOCKED c01_blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 unused_set=9 paper_schema=OK
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

- Parse C-01 / summarize lines above for outage_alert, tape_frozen, ETA pause,
  and promotion blockers. Do not promote Tier-2 while health is STALE or
  holdouts are thin.
- Live capital / size increases remain human-only (`ESCALATE.md`).
