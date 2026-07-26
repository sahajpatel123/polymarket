# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of 2026-07-26 (UTC)

Generated: `2026-07-26T07:51:41Z` (via `scripts/write_weekly_report.py`)

### System

| Item | Status |
|------|--------|
| Branch | `git log -1` → `93cf558 Add AS-path status board and show c_vol EV is inert (T1-155).` |
| Paper trading | `(no polymaker run process)` |
| Loop | 15m quant-edge cadence; Tier-2 gated on hours + paper gate |
| Tier-1 changelog lines | `209` (from `CHANGELOG_AGENT.md`) |
| Tier-1 backlog done | `155` (from `BACKLOG.md` Status: done) |

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| 1 | see `PENDING_REVIEW.md` |

Open candidates: `docs/STRATEGY_CANDIDATES.md` (C-01…C-04).

### Quantitative edge scoreboard

`TECHNIQUE_INVENTORY` + AS path (this cycle):

```
n=14 evidence_yes=0 mixed=0 partial=2 no=12
AS path blocked on current tape (n_through=0; conservative equal-price skip; finding requires fills). Board: scripts/as_path_status.py
id=microprice wired=yes evidence=no
id=ewma_fv_vol wired=yes evidence=partial
id=flow_nudge_fv wired=yes evidence=no
id=kalman_mid wired=intel-only evidence=no
id=signal_blend_calibration wired=no evidence=no
id=avellaneda_stoikov wired=opt-in evidence=no
id=kelly_fractional wired=opt-in evidence=no
id=kyle_lambda wired=fed+opt-in evidence=partial
id=vpin wired=fed evidence=no
id=garch_vol wired=no evidence=no
id=ofi_skew wired=fed evidence=no
id=covariance_sizing wired=no evidence=no
id=markout_toxicity wired=yes evidence=no
id=join_best_bid wired=opt-in evidence=no
```

### Outage / gate snapshot

`logs/outage_status.json`:

```
ts=2026-07-23T05:20:03.684745+00:00
connectivity=status=DOWN rest_ok=False ws_ok=False
outage_open=True
outage_total_h=13.8696
outage_alert=True
outage_alert_severe=True
outage_alert_prolonged=True
outage_alert_critical=True
outage_alert_imminent=False
outage_alert_final=False
outage_alert_critical_aged=True
outage_alert_critical_hour=True
operator_mode=CRITICAL_OPEN
operator_action=await_UP_then_full_recovery
operator_recovery_cmd=uv run python scripts/await_polymarket_recovery.py --once
frozen_tape_snapshot=logs/frozen_tape_snapshot.json
frozen_tape_status=FROZEN
hours_to_critical=0.0
minutes_to_critical=0
hours_to_imminent=0.0
outage_started_at=2026-07-22T15:27:53.062218+00:00
outage_critical_at=2026-07-23T03:27:53.062218+00:00
outage_critical_since=2026-07-23T03:28:21.518473+00:00
hours_past_critical=1.8617
minutes_past_critical=112
outage_imminent_since=None
hours_in_imminent=None
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
last_requote_age_s=49691.62
last_quote_age_s=50283.207
last_requote_at=2026-07-22T15:21:56.637370+00:00
last_quote_at=2026-07-22T15:21:56.636370+00:00
health=STALE
ensure_status=NEEDS_RESTART
collector_pid=78216
collector_pids=[78216]
n_cycles=109
c01_status=BLOCKED
c01_blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin
paper_log=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-22
paper_log_files=2
metrics_log=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl
```

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
log_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.pre12h.1784925687.31229
log_files=4
log_paths=/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl,/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-22,/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.2026-07-24,/Users/sahajpatel/Code/polymarket/livecfg/logs/paper.jsonl.pre12h.1784925687.31229
metrics_path=/Users/sahajpatel/Code/polymarket/livecfg/logs/metrics-paper.jsonl
status=OK lines=9161 json_lines=9161 bad_lines=0
runtime_basis=requote
runtime_hours=65.4302
runtime_hours_all_events=65.4329
quote_events=2429
requote_lines=4066
quotes_for_gate=2429
tier2_allowed=true reason=ok
```

`uv run python scripts/paper_metrics.py`:

```
status=OK quotes=20 cancels=2 fills=0 marks=43 realized_spread_usdc=0.000000
```

`uv run python scripts/shadow_adverse_selection.py`:

```
status=OK lifetimes=17 crossed_frac=0.0000 mean_edge=0.002391 markout_30s=-0.000000 n30=2
```

`uv run python scripts/paper_regime_report.py`:

```
status=OK requotes=0 trending_frac=0.0 false_trending_frac=0.0 false_trending_attr_frac=None false_trending_cancel_share=0.0 false_trending_place_share=0.0 vol_only_frac=None quiet_vol_max=None quiet_vol_p90=None trend_vol_min=None trend_vol_p50=None vol_gap=None suggested_vol=None path={} cancel_per_place=None transitions=0
```

`uv run python scripts/c01_promotion_checklist.py`:

```
status=BLOCKED blockers=health_ok,outage_closed,oos_replicated,holdout_not_thin runtime_h=65.4302 quotes=2429 health=STALE last_requote_age_s=113169.155 outage_open=True outage_total_h=88.3969 outage_alert=True outage_alert_severe=True outage_alert_prolonged=True outage_alert_critical=True outage_alert_imminent=False outage_alert_final=False outage_alert_critical_aged=True outage_alert_critical_hour=True oos=False thin=True vol_gap=None quiet_vol_max=None trend_vol_min=None suggested_vol=None suppress_2=None suppress_suggested=None suppress_target=None false_trending_attr_frac=None boundary_tight=None
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
status=OK cycles=110 runtime_h=8.37 hours_remaining=15.63 eta_wall_h=None eta_paused=True outage_open=True outage_total_h=13.8696 outage_alert=True outage_alert_severe=True outage_alert_prolonged=True outage_alert_critical=True outage_alert_imminent=False outage_alert_final=False outage_alert_critical_aged=True outage_alert_critical_hour=True hours_to_tier2_gate=15.63 hours_to_critical=0.0 minutes_to_critical=0 hours_to_imminent=0.0 outage_started_at=2026-07-22T15:27:53.062218+00:00 outage_critical_at=2026-07-23T03:27:53.062218+00:00 outage_critical_since=2026-07-23T03:28:21.518473+00:00 hours_past_critical=1.8617 minutes_past_critical=112 tier2_allowed=False quotes_per_wall_h=204.82 health=STALE last_requote_age_s=50285.327 tape_frozen=True connectivity=DOWN crossed_frac=0.0000 markout_30s=0.000006 false_trending_frac=0.0 false_trending_cancel_share=0.0 vol_only_frac=None vol_gap=None quiet_vol_max=None trend_vol_min=None suggested_vol=None false_trending_attr_frac=None c01=BLOCKED c01_blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0 unused_set=9 paper_schema=OK
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
- Quant-edge AS path: need through-price tape or Tier-2 equal-price policy;
  do not soften conservative fill matching for a metric.
