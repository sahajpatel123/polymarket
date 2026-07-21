# Autonomous Loop Protocol

Standing operating protocol for unattended cycles (days/weeks/months).
**Read in full every cycle.** Do not rely on memory of having read it before.

Remote: `https://github.com/sahajpatel123/polymarket.git`

## Long-run non-negotiable boundaries

1. **Tier-2 never auto-merges.** PR + `PENDING_REVIEW.md` only. Stagnation is
   safe; an unreviewed merge is not. Time passing does not authorize merge.
2. **Paper → live capital and any size/capital increase are human-only.** If you
   conclude "data justifies going live," write `ESCALATE.md` and stop.
3. **Weekly:** overwrite `WEEKLY_REPORT.md` (uptime, Tier-1 done count, pending
   Tier-2 PRs, paper P&L / risk metrics, dependency/security flags,
   credential/cert expiry). Passive-only.
4. **Weekly dependency/security audit** on a fixed schedule (see T1-07), not
   only when convenient.
5. **API / Gamma / CLOB contract interface changes that affect money movement
   are always Tier 2** (or escalate), even if the fix looks mechanical.

## Improvement backlog

Work from `BACKLOG.md` top-to-bottom within the allowed tier. Do not invent
open-ended improvements. New tasks must be added to `BACKLOG.md` first with
falsifiable done criteria.

## Rule 0 — You do not get to decide what counts as an improvement

A claim that something is "better" is only valid if it is the literal printed
output of a script you ran **this cycle**. If you cannot point to that command
and output, the claim does not exist.

## Tier system — classify BEFORE writing any code

### Tier 1 — safe, auto-commit allowed

- Tests, logging, monitoring/alerting, documentation, CI
- Behavior-preserving refactors (tests must still pass)
- Outside strategy, risk, and execution modules

### Tier 2 — gated, PR only, **never auto-merge**

- Strategy / pricing / inventory-skew / volatility estimation
- Execution / order submission
- Order sizing, position limits, market selection
- Kill-switch / dead-man / daily-loss — **harder than Tier 2: escalate**
- Wallet / private key / credential / `.env` handling — **escalate**

If unsure → Tier 2 (or escalate).

## Every-cycle procedure

1. Pull latest. Read `CHANGELOG_AGENT.md` and `BACKLOG.md` in full.
2. Classify intended work Tier 1 or Tier 2 before writing a line.
3. If Tier 2: run `uv run python scripts/paper_data_gate.py` (and metrics gate).
   Need ≥24h runtime **and** ≥500 new quotes since last Tier-2 merge — else
   do Tier 1.
4. Performance claims require script stdout in PR/commit message.
5. Tier 1: full test suite pass → commit + push. Fail → log verbatim in
   `CHANGELOG_AGENT.md`, stop that line.
6. Tier 2: branch + PR (metrics, sample window, script output, OOS reasoning).
   Do **not** merge. Add `PENDING_REVIEW.md` row.
7. Append **exactly one** line to `CHANGELOG_AGENT.md`.

## Anti-overfitting

Do not resubmit a cosmetically different Tier-2 idea against the same rejected
data window. Wait for a genuinely new window.

## Hallucination guard

Code claims need file path + line range. Performance numbers need this-cycle
script output. If unverified — stop, verify, or delete the claim.

## Hard escalation — write `ESCALATE.md` and stop

- Modifying kill-switch, dead-man-switch, or daily-loss-cap logic
- Touching wallet / private key / credential handling
- Changing the eval/backtest harness to improve a metric
- Unsure Tier 1 vs Tier 2
- Paper log corrupted / gapped / inconsistent
- Concluding live deployment or size increase is justified
- Polymarket/Gamma/CLOB interface change affecting money movement

No workarounds. An unread `ESCALATE.md` for weeks is a safe outcome.
