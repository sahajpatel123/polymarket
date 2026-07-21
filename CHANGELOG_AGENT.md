# CHANGELOG_AGENT

Append-only record of autonomous loop cycles. One line per cycle.

Format: `ISO8601 | Tier | description | evidence | outcome`

---

2026-07-21T20:57:00Z | Tier1 | Bootstrap agent docs/protocol; fix missing pytest-asyncio via uv sync --extra dev; no paper data (Tier2 gated) | `uv run pytest` → 111 passed, 2 skipped; paper.jsonl absent | merged
2026-07-21T21:10:00Z | Tier1 | T1-01 paper metrics logger + analyze script; wire engine quote/cancel/fill/mark; backlog+long-run protocol | `uv run pytest` + `tests/test_metrics.py`; `scripts/paper_metrics.py` on fixture | merged
2026-07-21T21:22:00Z | Tier1 | T1-02 deterministic journal replay harness → metrics JSONL analyzable by T1-01 | `uv run pytest tests/test_replay.py`; `scripts/replay_backtest.py` → n_quote=8 n_mark=5 | merged
2026-07-21T21:38:00Z | Tier1 | T1-03 alerting wrapper: five required kinds + verify script (thresholds untouched) | `scripts/verify_alerts.py` all_required_posted=true; pytest 125 passed | merged
