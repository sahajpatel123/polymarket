# 12h Paper Validation Report

Generated: `20260727T184300Z` UTC
Session start: `2026-07-27T14:42:56.595006+00:00`
Elapsed wall hours: **4.00** (target 12.0)
Profile: `live_scaled` · bankroll: **$30.00** · config: `livecfg`

## Data collected

| Stream | Lines since start |
|--------|------------------|
| paper.jsonl | 2551 |
| metrics-paper.jsonl | 988 |
| journal/paper.jsonl | 1324 |

## Offline backtest on collected journal

| Metric | Value |
|--------|-------|
| runtime_hours (journal) | 0.230677 |
| total_est_usdc | 0.192846 |
| period_return_pct | 0.00642821 |
| daily_return_pct | 0.66880205 |
| n_fill | 0 |
| estimate_is_reward_only | True |
| reward_pool_usdc | 2.771102 |
| reward_our_usdc | 0.192846 |
| spread_usdc | 0.0 |
| oob_check.ok | True (quotes=8, dust=0, oob=0) |

> **NOTE:** 0 fills in this window — returns are share-adjusted reward accrual only, not fill PnL. Paper fill sim still needs trade prints.

## Paper metrics (session slice)
```
t_rewards_usdc": 0.0,
    "reward_base_usdc": 0.0,
    "reward_conservative_usdc": 0.0,
    "reward_optimistic_usdc": 0.0,
    "rewards_daily_rate": 308.0,
    "total_fill_shares": 0.0,
    "undersized_in_band_seconds": 8438.079
  },
  "in_band_seconds": {
    "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75": 8438.079,
    "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919": 8370.371
  },
  "inventory_drift_abs_peak": 0.0,
  "inventory_net_end": {
    "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75": 0.0,
    "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919": 0.0
  },
  "log_loss": 0.61512,
  "markets": [
    "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75",
    "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919"
  ],
  "markout_mean": {
    "120s": 0.0,
    "300s": 0.0,
    "30s": 0.0
  },
  "markout_n": {
    "120s": 0,
    "300s": 0,
    "30s": 0
  },
  "mean_resting_notional_usdc": {
    "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75": 50.011775,
    "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919": 97.611741
  },
  "n_bad": 0,
  "n_cancel": 16,
  "n_dust_quotes": 0,
  "n_fill": 0,
  "n_in_band_quotes": 85,
  "n_lines": 988,
  "n_mark": 885,
  "n_oob_quotes": 0,
  "n_quote": 85,
  "path": "/Users/sahajpatel/code/polymarket/livecfg/12h_report/slice_20260727T184300Z/metrics-paper.jsonl",
  "realized_spread_usdc": 0.0,
  "rebate_pool_daily_usdc": {
    "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75": 93.18,
    "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919": 9.85
  },
  "reward_accrual_usdc": {
    "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75": 20.899871,
    "0x18b1c135d0a40c5894da9412e77311827d9caf16cf4cd6591b247a34730af919": 29.838822
  },
  "total_fill_shares": 0.0
}

status=OK quotes=85 cancels=16 fills=0 marks=885 realized_spread_usdc=0.000000
```

## Paper data gate
```
usage: paper_data_gate.py [-h] [--log LOG] [--metrics-log METRICS_LOG]
                          [--min-hours MIN_HOURS] [--min-quotes MIN_QUOTES]
paper_data_gate.py: error: unrecognized arguments: --config-dir livecfg
```

## Connectivity at report time
```
{
  "rest": [
    {
      "error": "URLError: <urlopen error timed out>",
      "latency_ms": 20021.6,
      "ok": false,
      "url": "https://clob.polymarket.com/time"
    },
    {
      "error": "URLError: <urlopen error timed out>",
      "latency_ms": 20011.0,
      "ok": false,
      "url": "https://gamma-api.polymarket.com/markets?limit=1"
    }
  ],
  "rest_ok": false,
  "status": "DOWN",
  "ws": {
    "error": "TimeoutError: timed out during opening handshake",
    "latency_ms": 10045.8,
    "ok": false,
    "url": "wss://ws-subscriptions-clob.polymarket.com/ws/market"
  },
  "ws_ok": false
}

status=DOWN rest_ok=False ws_ok=False
```

## How to interpret

- `period_return_pct` = total_est / bankroll over the **observed journal window**.
- `daily_return_pct` = period / (runtime_h/24) — extrapolation, not a calendar day guarantee.
- Runtime uses journal activity timestamps (not wall-clock).
- Compare to theoretical offline A/B on the same markets; large gaps mean tape/connectivity issues.

Artifacts: `livecfg/12h_report/slice_20260727T184300Z/`
