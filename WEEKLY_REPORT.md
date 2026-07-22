# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of 2026-07-22 (UTC)

Generated: `2026-07-22T19:10:00Z`

### System

| Item | Status |
|------|--------|
| Branch | `strategy-pricing` / `main` @ `1408926` (T1-65 tape_frozen) |
| Paper trading | **collector alive** (PID 78216) but **STALE** — Polymarket REST+WS DOWN ~3.7h; quotes frozen at 5529; `tape_frozen=true` |
| Loop | 10m Agent-1 strategy-pricing cadence; Tier-2 gated on hours; ETA paused |

### Tier-1 completed (this build-out window)

T1-01 … T1-65 `done` (metrics, replay, compare/synth, gate, shadow AS, knob
audit, richest-log, cycle history, C-01 checklist/counterfactual/vol context,
outage tooling, recovery append, tape_frozen).

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| 0 | 0 (`PENDING_REVIEW.md` empty) |

Open candidates (not promoted): C-01 `trend_vol_ratio`, C-02 scanner vs
realized, C-03 multi-knob null screen, C-04 unused knobs — see
`docs/STRATEGY_CANDIDATES.md`.

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
log_path=.../livecfg/logs/paper.jsonl
status=OK
runtime_basis=requote
runtime_hours=8.3700
runtime_hours_all_events=12.1498
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
status=OK requotes=2843 trending_frac=0.042912 false_trending_attr_frac=1.0
vol_only_frac=1.0 quiet_vol_max=1.989 trend_vol_min=2.029 vol_gap=0.04
suggested_vol=2.489 path={'missing_vol': 102, 'vol_only': 20}
```

`uv run python scripts/c01_promotion_checklist.py`:

```
status=BLOCKED blockers=hours_ok,health_ok,outage_closed,oos_replicated,holdout_not_thin
runtime_h=8.3700 quotes=5529 health=STALE outage_alert=True
suppress_2=0.0 suppress_suggested=0.1875 suppress_target=1.0
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
status=OK cycles=59 runtime_h=8.37 hours_remaining=15.63 eta_paused=True
outage_open=True outage_total_h≈3.68 tape_frozen=True c01=BLOCKED
```

### Dependency / security audit

`uv run python scripts/deps_audit.py`:

```
status=FLAGS packages=83 flagged=21 bumps=2
```

Most flags are informational `unpinned_direct` ranges. `ok=false` due to
baseline drift (`bumps=2`) — track; not a strategy change.

### Credentials / certificates

No expiry tracker in-repo. `.env` is gitignored; operator must rotate
`PK` / builder creds outside this loop. No cert files managed by polymaker.

### Blockers (informational)

- **Polymarket REST+WS outage** (~3.7h open): paper health STALE; requote
  runtime frozen at 8.37h; do not restart collector until UP
  (`SKIPPED_UPSTREAM_DOWN`). Recovery:
  `await_polymarket_recovery.py` (restarts + appends cycle).
- Tier-2 pricing/selection merges still need ≥24h **requote** runtime
  **and** OOS replication on a non-thin holdout. ETA paused while STALE.
- C-01 offline: suppress@2=0 / @suggested≈0.19 / @8=1.0 — still no PR
  while outage + thin holdout.
- Paper mode still has **0 fills** — reward path is liquidity-accrual only;
  classic markouts empty; shadow AS on frozen tape is not live signal.
- Live capital / size increases remain human-only (protocol boundary).
