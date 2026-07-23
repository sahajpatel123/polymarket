# PENDING_REVIEW

Tier-2 PRs waiting for human check-in. Autonomous loop opens PRs here; it does
not merge them.

| Opened | Branch / PR | Summary | Evidence |
|--------|-------------|---------|----------|
| 2026-07-23 | strategy-acceleration | Advanced quoting models: Avellaneda-Stoikov optimal pricing + Kelly-inspired sizing + risk-parity capital allocation. 42 new tests pass. Benchmark: advanced model is 1.91× faster at p99 than simple model. Does NOT modify live engine path — new pure-function modules available for opt-in use. | `pytest 301 passed, 2 skipped; ruff ok; mypy pre-existing only` |

