# Market selection (catalog / scanner / scoring)

Discovers political markets via Gamma, scores them for reward + rebate
attractiveness vs spread/extremity risk, and persists the ranked catalog to
SQLite. Selection into the live trade list is **manual** (`markets.toml`).

## Pipeline

```
polymaker scan
  → GammaClient.iter_markets (politics tag, liquidity filter)
  → parse_market (+ CLOB reward rates)
  → optional rewards_only filter
  → score_market → CatalogStore.upsert_market
  → export markets.csv
```

| Piece | Path |
|-------|------|
| Gamma client / parse | `catalog/gamma.py` |
| Scanner orchestration | `catalog/scanner.py` |
| Scoring | `catalog/scoring.py` |
| SQLite catalog | `catalog/store.py` |
| CLI | `cli.py` → `scan`, `markets`, `markets-add`, `export-csv` |

## Filters (`ScanConfig`)

- Default tag: politics (`POLITICS_TAG_SLUG` in `gamma.py`)
- `min_liquidity` (CLI default 1000 USDC)
- `rewards_only=True` unless `polymaker scan --all`
- Markets that fail `parse_market` (missing tokens / bad shape) are dropped

## Score formula

`score_market` in `catalog/scoring.py`:

```
reward_density   ≈ rewards_daily_rate × min(1, quote_size / liquidity)
rebate_potential ≈ vol_24h × (taker_fee_bps/1e4) × (1 − mid) × rebate_rate
income           = reward_density + rebate_potential × our_share
penalty          = (1 − 0.5·extremity) / (1 + 20·spread)
viability        = min(1, liquidity / 2000)
score            = income × penalty × viability
```

Extremity is 0 near mid 0.5 and → 1 near 0/1 (asymmetric payoff markets are
discounted). Viability stops near-empty books from ranking high via a huge
"our share" of a tiny pool.

These are **ranking signals**, not forecasts of earned dollars. Live reward
competition and your actual fill share are not modeled.

## From catalog to quoting

1. `polymaker scan` / `markets` — browse ranked catalog
2. `polymaker markets-add <slug> --profile <name>` — append to `markets.toml`
3. Engine resolves each enabled entry from the catalog (or live Gamma fallback),
   refreshes reward/fee/end-date metadata at startup and every
   `catalog_refresh_s`, and only then quotes

The engine does **not** auto-add markets from the scanner. Editing
`markets.toml` while running does not hot-reload: `Config.reload_markets`
exists and `watchfiles` is a dependency, but nothing in the engine watches the
file.
