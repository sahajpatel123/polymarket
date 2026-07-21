# Order reconciliation and churn tolerance

The strategy emits a desired resting set (`TargetQuotes`). Reconciliation
turns that into the smallest cancel/place plan against live orders so we do
not burn queue position on noise.

## Pure diff

**File:** `execution/reconciler.py` → `reconcile(targets, live, tick, reprice_ticks, resize_frac)`

For each target quote, look for a live order on the same `(token_id, side)`
where:

- `|live.price − target.price| ≤ reprice_ticks · tick`
- `|live.size − target.size| ≤ resize_frac · target.size` (if target size > 0)

Matched live orders are kept. Unmatched targets are placed. Live orders with
no match are cancelled.

Returns `ReconcilePlan{to_cancel, to_place}`. No I/O — the engine / gateway
execute the plan.

## Where tolerances come from

Profile knobs (`StrategyProfile`):

| Knob | Role |
|------|------|
| `reprice_ticks` | Ignore FV/microprice flicker within N ticks |
| `resize_frac` | Ignore size flips below this fraction (e.g. TRENDING half-size) |

Tuned sticky on thin/noisy reward markets so cancels are rare (see `TIPS.md`).

## Engine execution path

In `Engine._recompute_locked`, after `construct_quotes`:

1. Load live orders for YES+NO tokens from `StateStore`
2. `plan = reconcile(...)`
3. If noop → maybe merge YES+NO pairs; return
4. Cancel first; on cancel failure, REST-refresh that market and skip placing
5. Place remaining targets (unless load-shed under rate pressure in QUIET/TRENDING)
6. Partial place → quarantine (cancel asset + resync) to avoid untracked orders

Periodic REST reconcile (`_reconcile_loop`, every `reconcile_interval_s`)
overwrites local open-order state from the exchange. Young local orders
(`grace_s`, default 10s) survive a lagging snapshot so we do not double-place.

## Paper-mode interaction

In `--paper`, `ExecutionGateway.place` fabricates `paper-*` order ids and the
engine upserts them into `StateStore`. `open_orders()` returns `[]`, so the
periodic REST reconcile will eventually drop paper orders older than the grace
window; the next requote re-places them. Paper is a **pipeline dry-run against
the live book**, not a fill simulator.
