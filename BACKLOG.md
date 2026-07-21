# BACKLOG — autonomous loop work queue

Status values: `todo` | `in-progress` | `done` | `rejected-with-reason`.
Pull the next incomplete item top-to-bottom within the tier you are allowed
to work this cycle. Every item has falsifiable done criteria — do not mark
done without evidence (script output / tests) from that cycle.

## Tier 1 — infrastructure

### T1-01 Paper-trading metrics logger
- Status: `done`
- Done when: every quote update, fill, and cancel is logged with timestamp,
  market ID, side, price, size, and resulting inventory; a script can compute
  realized spread captured, adverse-selection cost (30s / 2min / 5min after a
  fill), inventory drift over time, and reward/rebate accrual per market,
  purely from these logs.
- Evidence path: `logs/metrics-paper.jsonl`, `scripts/paper_metrics.py`,
  `src/polymaker/metrics/`; verified `tests/test_metrics.py`

### T1-02 Deterministic backtest/replay harness
- Status: `done`
- Done when: a script replays a historical order-book/log window through the
  current strategy code and reproduces the same metrics as T1-01, with no live
  connection required.
- Evidence: `src/polymaker/replay/`, `scripts/replay_backtest.py`,
  `tests/test_replay.py`

### T1-03 Alerting wrapper
- Status: `todo`
- Done when: process crash, kill-switch trigger, daily-loss-cap trigger,
  WebSocket disconnect beyond N seconds, and API auth failure each produce an
  immediate message to a configured endpoint, verified by deliberately
  triggering each condition in paper mode.

### T1-04 Structured logging
- Status: `todo`
- Done when: all logs are JSON with consistent fields, rotated, and greppable
  by market ID and time range. (Partial: structlog JSON file exists; rotation
  and market-ID greppability still incomplete.)

### T1-05 Test suite for deterministic components
- Status: `todo`
- Done when: order reconciliation, quote-generation math, and config parsing
  each have unit tests covering documented edge cases (zero inventory, max
  inventory, missing market data, disconnect mid-quote). (Partial suite exists;
  edge-case matrix not fully documented/covered.)

### T1-06 CI pipeline
- Status: `todo`
- Done when: every commit automatically runs the full test suite and reports
  pass/fail before merge is possible.

### T1-07 Dependency audit script
- Status: `todo`
- Done when: a script checks every dependency against pinned versions/hashes
  and flags anything with post-install scripts or unexplained version bumps.

### T1-08 Local metrics dashboard
- Status: `todo`
- Done when: the metrics from T1-01 are visualized simply enough that a human
  glancing at it once or twice a day can assess system health without reading
  raw logs.

## Tier 2 — strategy / execution (PR only; never auto-merge)

Requires T1-01 + T1-02, ≥24h paper runtime and ≥500 new quotes since last
Tier-2 merge. Human reviews via `PENDING_REVIEW.md`.

### T2-01 Market selection / reward-ranking refinement
- Status: `todo`
- Done when: candidate ranking backtested across multiple markets shows
  improved reward-to-risk vs current; script output attached to PR.

### T2-02 Volatility/toxicity estimator improvement
- Status: `todo`
- Done when: candidate vs current on same window by adverse-selection cost per
  fill (script output).

### T2-03 Inventory-skew curve tuning
- Status: `todo`
- Done when: backtest shows lower inventory drift at equal spread capture, OR
  higher spread capture at equal inventory risk — trade-off stated explicitly.

### T2-04 Regime-detection refinement
- Status: `todo`
- Done when: backtested on labeled news-event windows — fewer adverse fills
  during events without excessively cutting quote uptime otherwise.

### T2-05 Quote churn / update-frequency tuning
- Status: `todo`
- Done when: backtest shows improved fill quality without excessive requoting.

### T2-06 Per-market and portfolio-level position limits
- Status: `todo`
- Done when: backtested limits bind before daily-loss cap across historical
  volatile windows. (**Does not** change kill-switch / daily-loss-cap values.)

## Never autonomous

Kill-switch thresholds, daily-loss-cap value, wallet/key handling, paper→live
capital, and position-size/capital increases — human only. Escalate to
`ESCALATE.md` if the loop starts reasoning otherwise.
