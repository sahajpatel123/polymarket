# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of 2026-07-22 (UTC)

Generated: `2026-07-22T13:30:00Z`

### System

| Item | Status |
|------|--------|
| Branch | `strategy-pricing` / `main` @ `23ac400` (T1-32 richest log discovery) |
| Paper trading | **running** — `livecfg` collector PID active since ~06:59Z; health OK |
| Loop | 10m Agent-1 strategy-pricing cadence; Tier-2 gated on hours |

### Tier-1 completed (this build-out window)

T1-01 … T1-32 `done` (metrics, replay, compare/synth, gate, shadow AS, knob
audit, richest-log discovery, cycle history, candidates docs).

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| 0 | 0 (`PENDING_REVIEW.md` empty) |

Open candidates (not promoted): C-01 `trend_vol_ratio`, C-02 scanner vs
realized, C-03 multi-knob null screen — see `docs/STRATEGY_CANDIDATES.md`.

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
log_path=.../livecfg/logs/paper.jsonl
status=OK
runtime_hours=6.5124
quote_events=4318
requote_lines=2195
quotes_for_gate=4318
tier2_allowed=false reason=need_hours>=24.0
```

`uv run python scripts/paper_metrics.py`:

```
status=OK quotes=4318 cancels=87 fills=0 marks=14825 realized_spread_usdc=0.000000
```

`uv run python scripts/shadow_adverse_selection.py`:

```
status=OK lifetimes=4316 crossed_frac=0.0000 mean_edge=0.002511 markout_30s=0.000008
```

`uv run python scripts/paper_regime_report.py`:

```
status=OK requotes=2195 trending_frac=0.032802 cancel_per_place=0.020148 transitions=84
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
status=OK cycles=31 runtime_h=6.3305 hours_remaining=17.6695 eta_wall_h~17.7
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

- Tier-2 pricing/selection merges still need ≥24h paper runtime (ETA ~18
  wall-hours) **and** OOS replication on a non-thin holdout.
- Paper mode still has **0 fills** — reward path is liquidity-accrual only;
  classic markouts empty; use shadow AS until fills exist.
- Live capital / size increases remain human-only (protocol boundary).
