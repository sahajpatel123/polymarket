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
- Status: `done`
- Done when: process crash, kill-switch trigger, daily-loss-cap trigger,
  WebSocket disconnect beyond N seconds, and API auth failure each produce an
  immediate message to a configured endpoint, verified by deliberately
  triggering each condition in paper mode.
- Evidence: `scripts/verify_alerts.py` → all_required_posted=true;
  `tests/test_alerts.py`

### T1-04 Structured logging
- Status: `done`
- Done when: all logs are JSON with consistent fields, rotated, and greppable
  by market ID and time range. (Partial: structlog JSON file exists; rotation
  and market-ID greppability still incomplete.)
- Evidence: TimedRotatingFileHandler + required fields in `logging.py`;
  `scripts/grep_logs.py` / `polymaker.loggrep`; `tests/test_logging.py`

### T1-05 Test suite for deterministic components
- Status: `done`
- Done when: order reconciliation, quote-generation math, and config parsing
  each have unit tests covering documented edge cases (zero inventory, max
  inventory, missing market data, disconnect mid-quote). (Partial suite exists;
  edge-case matrix not fully documented/covered.)
- Evidence: `docs/EDGE_CASES.md`; `tests/test_config.py`; quoting/reconcile
  edge tests; disconnect covered by hardening cancel-failure test

### T1-06 CI pipeline
- Status: `done`
- Done when: every commit automatically runs the full test suite and reports
  pass/fail before merge is possible.
- Evidence: `.github/workflows/ci.yml` (pytest on push/PR to main); README notes
  required status check for merge gating

### T1-07 Dependency audit script
- Status: `done`
- Done when: a script checks every dependency against pinned versions/hashes
  and flags anything with post-install scripts or unexplained version bumps.
- Evidence: `scripts/deps_audit.py`, `deps/baseline.json`, `tests/test_deps_audit.py`;
  CI runs `--fail-on-flags`

### T1-08 Local metrics dashboard
- Status: `done`
- Done when: the metrics from T1-01 are visualized simply enough that a human
  glancing at it once or twice a day can assess system health without reading
  raw logs.
- Evidence: `scripts/metrics_dashboard.py` → `logs/dashboard.html`;
  `polymaker dashboard`; `tests/test_dashboard.py`

### T1-09 Strategy A/B compare harness (eval infra for Tier-2)
- Status: `done`
- Done when: a script replays one journal through baseline vs candidate
  StrategyProfile overrides, prints T1-01 metric deltas (spread, markout,
  inventory drift, reward accrual, quote/cancel counts), and supports a
  timestamp holdout slice for OOS scoring. No strategy math changes.
- Evidence: `src/polymaker/replay/compare.py`, `scripts/compare_strategies.py`,
  `tests/test_compare_strategies.py`

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

## Tier 1 — performance & execution latency (perf-agent)

### P1-01 Latency/throughput benchmark harness
- Status: `done`
- Owner: perf-agent
- Started: 2026-07-22T03:00:00Z
- Evidence: `scripts/bench_latency.py`, `tests/test_bench_latency.py`; 5 tests pass;
  baseline: replay p50=41.5us p95=71.2us p99=115.4us (26842 eps);
  pure strategy p50=12.4us p95=16.4us p99=16.7us (77584 ops/s)
- Owner: perf-agent
- Done when: a script measures, on a fixed replayed data window, the time from
  market-data-received to quote/order-submitted (p50/p95/p99), and orders-
  processed-per-second under load. Must exist before any optimization below.
- Evidence: `scripts/bench_latency.py`, `tests/test_bench_latency.py`

### P1-02 Profile the current hot path
- Status: `done`
- Owner: perf-agent
- Started: 2026-07-22T03:10:00Z
- Evidence: `perf/profile_2026-07-22.txt`; top 3 bottlenecks:
  1. OrderBook.view() — 0.083s cum (19.1%); _nth_bid/_nth_ask iterate full
     SortedDict even for n=0; depth_within uses generator+sum
  2. construct_quotes() — 0.068s cum (15.7%); _add_layers 0.024s, round() x39943
  3. reconcile() — 0.031s cum (7.1%); defaultdict + set lookups
- Done when: profiler output attached naming the top 3 actual bottlenecks —
  not assumed ones. Do not optimize what you assume is slow.
- Evidence: `perf/profile_<date>.txt` or inline cProfile output

### P1-03 Fix the top bottleneck, re-measure, repeat
- Status: `in-progress`
- Owner: perf-agent
- Started: 2026-07-22T03:20:00Z
- Target: OrderBook.view() — optimize _nth_bid/_nth_ask (n=0 fast path via
  peekitem), _top_size (islice to avoid full list), depth_within (direct loop)
- Done when: one bottleneck at a time, each with before/after benchmark numbers
  and golden-output diff attached. Tests + golden-output diff must pass.
- Evidence: per-change benchmark output + `tests/test_bench_latency.py`

### P1-04 Connection/network layer efficiency
- Status: `todo`
- Owner: perf-agent
- Done when: connection pooling, keep-alive, reduced round trips to the exchange
  API, once code-level bottlenecks are addressed.
- Evidence: benchmark output showing improvement

### P1-05 Build/runtime tuning
- Status: `todo`
- Owner: perf-agent
- Done when: concurrency and resource settings tuned, last, once code-level
  bottlenecks are addressed.
- Evidence: benchmark output showing improvement

## Never autonomous

Kill-switch thresholds, daily-loss-cap value, wallet/key handling, paper→live
capital, and position-size/capital increases — human only. Escalate to
`ESCALATE.md` if the loop starts reasoning otherwise.
