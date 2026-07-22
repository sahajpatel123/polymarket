# Escalation Log

Items that failed static verification checks. Each entry is a separate
failure. Do not resolve these here — flag and escalate.

---

## Entry 1 — Branch protection: PR reviews not required, enforce_admins false

**Check:** #1 GitHub branch protection on main
**Date:** 2026-07-22T07:13:40Z
**Severity:** HIGH

**What was found:**
Branch protection on `main` is partially configured. Status checks (pytest,
strict) are required, force pushes and deletions are blocked. However:

- `required_pull_request_reviews` is NOT configured — PRs can be merged
  without review.
- `enforce_admins` is `false` — admins can bypass all protection rules,
  including status checks.

**Expected:**
- PR review required (at least 1 reviewer)
- `enforce_admins` enabled (true)

**Action needed:**
Configure `required_pull_request_reviews` and set `enforce_admins: true`
on the `main` branch via GitHub repository settings.

---

## Entry 2 — No .env file: cannot verify API key scope or wallet balance

**Check:** #2 API key scope and wallet balance
**Date:** 2026-07-22T07:13:40Z
**Severity:** MEDIUM

**What was found:**
No `.env` file exists in the repository (only `.env.example`). No Polymarket
API keys (PK, BROWSER_ADDRESS, POLY_BUILDER_KEY/SECRET/PASSPHRASE) are
configured. Cannot verify:

- API key scope (read-only vs. trading)
- Wallet balance (pUSD holdings)
- Unexpectedly high balances

The `ANTHROPIC_API_KEY` is set in the environment (OpenRouter), but this is
the agent API key, not a Polymarket trading key.

**Expected:**
A `.env` file with configured PK and BROWSER_ADDRESS, allowing verification
of key scope and wallet balance.

**Action needed:**
Create `.env` from `.env.example` and populate PK and BROWSER_ADDRESS.
Then verify key scope and wallet balance are within expected bounds.

---

## Entry 4 — Cannot perform alert fire-drill

**Check:** #4 Alert fire-drill (monthly)
**Date:** 2026-07-22T07:13:40Z
**Severity:** HIGH

**What was found:**
No `.env` file exists, so `ALERT_WEBHOOK_URL` is not configured. No record
of a previous fire-drill exists (no prior `STATIC_CHECK_LOG.md` or
`ESCALATE.md`). Cannot perform the monthly fire-drill:

- Cannot trigger a crash, daily-loss cap, or WebSocket disconnect in paper
  mode without configured credentials.
- Cannot confirm alerts reach the configured endpoint.

**Expected:**
A configured `ALERT_WEBHOOK_URL` and `.env` file to run the fire-drill.
A record of the last fire-drill date to determine if the monthly cadence
is due.

**Action needed:**
1. Create `.env` with `ALERT_WEBHOOK_URL` configured.
2. Schedule and perform the monthly fire-drill (crash, daily-loss cap,
   WebSocket disconnect in paper mode).
3. Record fire-drill results and last-run date for future cycles.

---

## Entry 5 — Cannot fetch Polymarket terms of service

**Check:** #5 Polymarket terms of service
**Date:** 2026-07-22T07:13:40Z
**Severity:** MEDIUM

**What was found:**
Attempted to fetch `https://polymarket.com/legal/terms-of-service` via:

- `curl` — exit code 28 (timeout after 15-30 seconds)
- WebFetch tool — no output returned

The page appears to be a JavaScript SPA that does not return content via
curl. No previous ToS snapshot exists to diff against.

**Expected:**
A successful fetch of the ToS page, with the text saved for diff against
future checks.

**Action needed:**
1. Fetch the ToS via a JavaScript-capable method (browser, Playwright,
   etc.) and save a snapshot.
2. Establish a baseline snapshot for future diff checks.
3. Investigate why curl cannot reach the page (DNS, firewall, anti-bot).

---

## Entry 7 — Cannot check agent API spend

**Check:** #7 Agent API spend
**Date:** 2026-07-22T07:13:40Z
**Severity:** MEDIUM

**What was found:**
`ANTHROPIC_API_KEY` is set in the environment (OpenRouter). No budget
tracking or spend monitoring is configured in the project. Attempted to
query the OpenRouter credits endpoint
(`https://openrouter.ai/api/v1/credits`) but the Bash command was blocked
by the auto-mode classifier.

Cannot verify:

- Current API spend against a monthly budget
- Whether spend is within expected bounds
- Unexpectedly high spend

**Expected:**
A configured monthly budget and a method to check API spend (e.g.,
OpenRouter credits endpoint, or a spend-tracking script).

**Action needed:**
1. Configure a monthly API spend budget.
2. Set up a script or monitor to check spend against the budget.
3. Resolve the auto-mode classifier block on the OpenRouter credits
   endpoint query.

---

**Cycle 2 update (2026-07-22T07:48:53Z):** Partial progress. The OpenRouter
credits endpoint is now accessible. Current spend: $7.63 out of $18.75 total
credits (40.7%). However, no monthly budget is configured, so cannot
determine if this is within expected bounds. Also only covers Agent 3's
spend — cannot check Agent 1 and Agent 2 spend ("both loops"). Check
remains FAIL.
