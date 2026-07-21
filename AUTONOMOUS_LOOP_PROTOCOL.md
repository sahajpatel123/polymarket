# Autonomous Loop Protocol

Standing operating protocol for unattended cycles. Read in full every cycle.
Do not rely on memory of having read it before.

## Rule 0 — You do not get to decide what counts as an improvement

A claim that something is "better" is only valid if it is the literal printed
output of a script you ran this cycle. Not a memory of a past cycle. Not an
estimate. Not a plausible-sounding number. If you cannot point to a command you
ran and the exact output it produced, the claim does not exist. Discard it.

## Tier system — classify BEFORE writing any code

### Tier 1 — safe, auto-commit allowed

- Tests, logging, monitoring/alerting code, documentation, CI config
- Refactors that do not change behavior (existing test suite must still pass
  unchanged, byte-for-byte same inputs/outputs on any function you touch)
- Anything outside the strategy, risk, and execution modules

### Tier 2 — gated, PR only

- Any file in the strategy/pricing/inventory-skew/volatility-estimation path
- Any file in the execution/order-submission path
- Anything touching order sizing, position limits, or market selection logic
- Kill-switch, dead-man switch, daily-loss cap — see Hard escalation (stricter)
- Wallet, private key, credential, or `.env` handling of any kind

If unsure, treat as Tier 2.

Remote for commit/push: `https://github.com/sahajpatel123/polymarket.git`

## Every-cycle procedure

1. Pull latest. Read `CHANGELOG_AGENT.md` in full.
2. Classify intended work as Tier 1 or Tier 2 before writing a line.
3. If Tier 2: query paper-trading logs directly. If fewer than **24 hours** of
   runtime OR fewer than **500** newly logged quotes since last Tier-2 merge,
   do Tier 1 instead.
4. Performance claims require script output pasted into PR/commit message.
5. Tier 1: full test suite must pass, then commit + push. On failure: log
   verbatim in `CHANGELOG_AGENT.md` and stop that line of work.
6. Tier 2: new branch + PR with metrics/sample/script output/reasoning. Do not
   merge. Add `PENDING_REVIEW.md` entry.
7. Append exactly one line to `CHANGELOG_AGENT.md` this cycle.

## Anti-overfitting

Do not resubmit a cosmetically different Tier-2 idea against the same rejected
data window. Wait for a genuinely new data window.

## Hallucination guard

Every factual claim about the codebase must cite exact file path and line
range. Every performance number must be script output from this cycle.

## Hard escalation — write `ESCALATE.md` and stop

- Modifying kill-switch, dead-man-switch, or daily-loss-cap logic
- Touching wallet / private key / credential handling
- Changing the evaluation/backtest harness to improve a metric
- Unsure whether something is Tier 1 or Tier 2
- Paper-trading log looks corrupted, gapped, or inconsistent
