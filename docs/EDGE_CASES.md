# Deterministic edge-case matrix (T1-05)

Falsifiable coverage for order reconciliation, quote construction, and config
parsing. Each row maps to a unit test. Update this table when adding cases.

## Quote generation (`strategy/quoting.py`)

| Case | Expected | Test |
|------|----------|------|
| Zero inventory (flat) | Two-sided BUY YES + BUY NO | `test_quiet_market_quotes_both_sides_as_bids` |
| Max / soft-cap inventory | Adding side pulled when `u >= q_soft_frac` | `test_max_inventory_pulls_adding_side` |
| EVENT / HALTED | Empty target set | `test_event_and_halted_pull_all_quotes` |
| Missing / one-sided book view | No crash; may skip bids that can't place | `test_missing_book_view_does_not_crash` |
| Crossed book (engine) | Skip recompute / no new quotes | `test_crossed_book_skips_quoting` (hardening2) |
| Dust position | No SELL exit | `test_no_exit_when_position_is_dust` |

## Order reconciliation (`execution/reconciler.py`)

| Case | Expected | Test |
|------|----------|------|
| No live orders | Place all targets | `test_reconcile_places_when_no_live` |
| Within reprice_ticks | Keep live | `test_reconcile_keeps_close_order` |
| Beyond reprice_ticks | Cancel + place | `test_reconcile_reprices_when_far` |
| Size drift > resize_frac | Cancel + place | `test_reconcile_resizes_when_size_drifts` |
| Empty targets | Cancel all live | `test_reconcile_cancels_all_when_target_empty` |
| Exact reprice boundary | Keep when `abs(dp) == reprice_ticks * tick` | `test_reconcile_keeps_at_exact_reprice_boundary` |
| Disconnect mid-quote (engine) | Cancel failure → no place this cycle | `test_cancel_failure_keeps_orders_and_skips_placement` |

## Config parsing (`config.py`)

| Case | Expected | Test |
|------|----------|------|
| Missing markets.toml | Empty markets list | `test_load_missing_files_defaults` |
| Unknown profile key | `extra=forbid` → ValidationError | `test_strategy_profile_rejects_unknown_key` |
| Market without slug/cid | ValidationError | `test_market_entry_requires_identifier` |
| Per-market overrides | `with_overrides` applies known keys only | `test_profile_overrides_apply` |
| Enabled filter | `enabled_markets` skips disabled | `test_enabled_markets_filter` |
