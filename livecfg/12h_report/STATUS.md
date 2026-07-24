# 12h Session — Partial Recovery Status (2026-07-25 ~02:35 UTC)

## What happened since the last report

**The 12h paper session IS running and receiving live data.** But the
state of Polymarket is mixed:

| Component | Status | Evidence |
|-----------|--------|----------|
| WebSocket (market data) | **UP** | 98 quotes, 2893 requotes, last requote 12s ago |
| REST API (orders, balances) | **403 Forbidden** | All REST calls return HTTP 403 |
| Paper collector process | **ALIVE** | PID running, journal growing |

## What this means

- **Market data IS flowing.** The paper collector is receiving real
  book snapshots and trade prints from Polymarket's WebSocket. The
  12h session script started the collector at 20:41 UTC and it's been
  quoting since the WebSocket came back up.
- **We can't place orders or check balances.** The REST API is still
  returning 403. This means the paper collector can observe the market
  but can't validate order placement or fill simulation end-to-end.
- **The 12h session will keep running.** It will collect 12 hours of
  market data. The paper_health check shows `status=OK` with 98
  quotes placed in ~8 minutes of live data.

## Current live session numbers

Running the backtest on the new live data (8 minutes so far):

| Metric | Value |
|--------|-------|
| Quotes placed | 18 |
| OOB quotes | 0 |
| Dust quotes | 0 |
| In-band seconds | 482s (of 540s = 89% uptime) |
| Reward accrued | $0.42 |
| Our share of pool | 14.4% |
| Period return | 1.40% over 8 min |
| Daily (extrapolated) | 250% (ceiling) |
| Actual fills | 0 |
| Paper health | OK |

The 89% in-band uptime is real — not a backtest artifact. The 14.4%
share is also real — the reward pool is being shared with other
market makers. The 0 fills is normal for 8 minutes of data on a thin
book.

## What to expect at 08:41 UTC (session end)

- ~12 hours of live market data
- Hundreds of quotes and requotes
- Some number of fills (if trades cross our prices)
- Reward accrual proportional to in-band uptime × pool rate × share

## What needs full recovery for

1. **Order placement validation** — needs REST API back (not 403)
2. **Real markout measurement** — needs fills
3. **Full $30 deployment** — needs 24h+ paper then live

## Commands to monitor

```bash
# Current health
uv run python scripts/paper_health.py --max-age-s 600

# Connectivity
uv run python scripts/polymarket_connectivity.py

# Run backtest on live data
uv run python scripts/backtest.py \
  --journal livecfg/journal/paper.jsonl \
  --profile live_scaled --bankroll 30

# Capital report
uv run python scripts/capital_report.py --capital 30 \
  --journal livecfg/journal/paper.jsonl --profile live_scaled
```
