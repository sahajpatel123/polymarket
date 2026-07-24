# 12h Paper Validation — RUNNING

**Started:** 2026-07-24T20:41:27Z UTC  
**Planned end:** ~2026-07-25T08:41:27Z UTC (12 hours)  
**Profile:** `live_scaled`  
**Bankroll:** $100  
**Markets:** Newsom 2028 Dem + Vance 2028 GOP  
**Config:** `livecfg/`

## Processes
- Session wrapper: `scripts/paper_12h_session.sh` (sleeps until end, then auto-report)
- Paper collector: `polymaker run --paper --config-dir livecfg`
- PID file: `livecfg/12h_paper.pid`
- Session marker: `livecfg/12h_session.json`

## Logs
- Collector stdout: `livecfg/logs/collector-12h-stdout.log`
- Heartbeats (30m): `livecfg/logs/12h_heartbeat.log`
- Paper / metrics / journal: `livecfg/logs/`, `livecfg/journal/`

## Report (auto-generated at end)
- `livecfg/12h_report/REPORT_LATEST.md`
- Per-run slice under `livecfg/12h_report/slice_*/`

## Note at start
Polymarket REST/WS was **DOWN** at launch (timeouts). Paper engine started and will **retry reconnects**. If the outage lasts, the 12h wall window may include long STALE periods — the report will show actual journal runtime and connectivity at end.

## When you wake up
```bash
cat livecfg/12h_report/REPORT_LATEST.md
# or force report now:
uv run python scripts/paper_12h_report.py --session livecfg/12h_session.json
```
