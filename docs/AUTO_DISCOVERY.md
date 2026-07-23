# Auto Market Discovery — Self-Updating Trade List

The engine can automatically discover and trade new markets without manual
intervention. This document covers the two mechanisms: **periodic discovery**
(scans Gamma for new markets) and **hot-reload** (watches `markets.toml` for
edits).

## Quick start

Enable auto-discovery by editing `config/config.toml`:

```toml
[engine]
auto_discovery_enabled = true
auto_discovery_interval_s = 3600.0    # hourly re-scan
auto_discovery_tags = ["politics", "sports"]   # scan multiple categories
auto_discovery_min_score = 0.01        # filter junk markets
auto_discovery_max_markets = 20        # cap on total auto-added markets
auto_discovery_profile = "political-longdated"
auto_discovery_hot_reload = true       # watch markets.toml
```

Then start the engine normally: `uv run polymaker run --paper`.

## How it works

### 1. Periodic discovery (`auto_discovery_loop`)

Every `auto_discovery_interval_s` seconds (default 1 hour), the engine:

1. Runs the scanner against all configured tags (`auto_discovery_tags`)
2. Scores each scanned market using the existing `score_market` formula
3. Filters by `auto_discovery_min_score` (default 0.01)
4. For each new market passing the filter:
   - Fetches fresh metadata from Gamma
   - Calls `Engine.add_market()` to:
     - Register per-market state (estimators, regime, lock, dirty event)
     - Subscribe the WebSocket to its tokens
     - Add to the user-stream market set (live mode)
     - Spawn a supervised quoter task
     - Emit a `market_meta` metric with `auto_discovered=true`
5. Checks all currently-tracked markets via `markets_by_condition` and
   removes any that are now `closed=true` or `acceptingOrders=false`

Markets already in the trade list are not re-added. Markets that fail
to resolve are skipped silently. The cap `auto_discovery_max_markets`
limits the total number of auto-discovered markets (manual entries in
`markets.toml` are not counted).

### 2. Hot-reload (`hot_reload_loop`)

Uses `watchfiles` to monitor `markets.toml` for manual edits. On any
change, the engine:

1. Reloads the file via `Config.load()`
2. Computes the diff between the file and the engine's tracked set
3. **Added** in the file → `add_market()` (fetches from Gamma if not in catalog)
4. **Removed** from the file → `remove_market()` (cancels orders, drops state)

Debounced by 0.5s so multi-line edits don't trigger a flood.

## Engine API

Three new public methods on `Engine`:

```python
# Dynamic add
await engine.add_market(meta, profile)   # returns True on fresh add

# Dynamic remove
await engine.remove_market(cid)          # returns True if removed

# Manual reconcile (called by hot_reload_loop automatically)
await engine._reconcile_market_list()    # forces a diff against markets.toml
```

`MarketDataService` also gained:

```python
svc.add_market(condition_id, [token_ids])    # dynamic subscribe
svc.remove_market(condition_id)              # dynamic unsubscribe
```

When the WS is already connected, `add_market` sends a fresh subscribe
message so the server immediately starts streaming the new tokens.

## Safety

| Risk | Mitigation |
|------|------------|
| Market spam / scam markets | `auto_discovery_min_score` filters junk |
| Too many markets | `auto_discovery_max_markets` hard cap |
| Resource exhaustion | Same cap + WS connection pooling |
| Stale market (closed/resolved) | Auto-pruned on every discovery pass |
| Auto-add a market you don't want | Set `auto_discovery_enabled = false` |
| Manual edit fights with auto-add | Hot-reload only acts on manual entries; auto-added markets are removed only when closed |

## Trade-off with reward farming

`newsom-mm` and `romania-pm` profiles are tuned for **specific** markets
(Newsom, Romania PM) based on live microstructure. Auto-discovery assigns
`auto_discovery_profile` (default `political-longdated`) to new markets,
which is general-purpose but not optimized for any one.

For reward farming on a specific market, list it explicitly in
`markets.toml` with the tuned profile; let auto-discovery handle the
rest.

## Migration path

1. **Validate in paper mode** — set `auto_discovery_enabled = true` in
   `config.toml`, run `uv run polymaker run --paper`, watch the logs for
   `auto_market_added` events. After 24h, check `scripts/paper_metrics.py`
   to see if the auto-added markets are profitable.

2. **Tune the score filter** — set `auto_discovery_min_score` to filter
   out low-reward markets. Use `scripts/rank_vs_realized.py` to see
   which scores correlate with realized reward.

3. **Go live with tiny size** — once paper shows positive PnL, switch
   to `uv run polymaker run` with small `bankroll_usdc` and watch.

4. **Scale up** — after a week of stable live operation, increase the
   `max_market_notional_usdc` and `max_total_exposure_usdc` caps.

## What's NOT changed

- The scanner logic itself is unchanged (uses the same `score_market` formula)
- Live order placement, reconciliation, risk management are unchanged
- The simple/advanced quoting models are unaffected
- The fill simulator (paper mode) is unaffected
- All existing tests still pass (305 passed, 2 skipped)
