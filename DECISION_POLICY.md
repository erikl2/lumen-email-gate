# Decision policy: Lumen Cloud outbound email gate

**Persona:** customer support lead. **Gated action:** `send_email` to a customer. **Source of truth:** `price_sheet.csv` and `policies.json`.
**Principle:** an email is a promise in writing. We block what our data proves wrong, send to a person what isn't provably wrong but isn't ours to promise, and send everything else, including emails with nothing to check.

## Signals

| # | Signal | What it checks | Independent of Ezio? |
|---|---|---|---|
| 1 | **Source-of-truth claim check** (`wrapper/claims.py`) | Every price, annual/loyalty discount, stacking, refund window and scope, trial length/plans/card, first-response time, Enterprise pricing, and loyalty tenure statement, compared with the data. Reads figures in any format ("nineteen dollars", "$0.099 thousand", "two weeks", "a fifth off", "24hrs") and links each one to the plan named in its clause or the customer's plan. | Yes. Runs first, with no network, so it still works when Ezio is down |
| 2 | **Discount eligibility** | A loyalty discount *offered to this customer* needs plan ∈ {Pro, Business} and tenure ≥ 12 months (from `context`). | Yes |
| 3 | **Authority and style rules** | Feature/fix/release promises, delivery dates, exceptions and goodwill concessions, guarantees, hype, strong flattery. Negations ("I can't promise…", "discounts can't be combined") are handled. | Yes |
| 4 | **Ezio per-claim fact-check** | `claims[]` verdicts for public-knowledge claims (laws, regulations) our files don't cover. | Ezio |
| 5 | **Ezio dimensions** | `tone`, `bias`, `agreement`, each with its own threshold, because a GREEN tier can hide one weak dimension. | Ezio |
| 6 | **Ezio tier** | Backstop only. GREEN never approves on its own. | Ezio |

## Decision ladder (strictest reason wins)

| Decision | When | Why |
|---|---|---|
| **BLOCK** | Any statement contradicts our data (signals 1–2), or Ezio marks a claim **contradicted** | We'd have to honor or retract it in writing (rules 1, 2, 6). Blocked drafts can't be overridden in the UI; they must be fixed and re-checked. |
| **HOLD_FOR_REVIEW** | A promise, exception, guarantee or hype phrase; a dimension below threshold; an Ezio **unverifiable** claim; tier YELLOW, or RED without a contradiction; a figure we can't tie to one plan (e.g. an annual price without "billed annually"); Ezio down or accuracy unavailable | Not provably false, but not the agent's call (rules 3, 4). A manager releases or rejects it with a logged reason. |
| **APPROVE** | Everything checkable matched, and Ezio saw nothing wrong. **An email with no checkable claims is approved** (rule 5); an Ezio-**supported** claim needs no second opinion (rule 6). | Over-blocking costs customers and agents time. |

## Thresholds, one sentence each

- **Tone hold below 70.** One friendly "!" or acronym costs 6–10 points and is normal in support email. Below 70 means three or more promotional signals, which rule 4 sends to review.
- **Bias hold below 75.** One "clearly" (−15) is harmless. Two one-sided phrases (≤70) mean the email argues instead of informing.
- **Agreement hold below 70.** Each flattering phrase costs 25. Two or more means the email is telling the customer what they want to hear. Our own check also flags strong single phrases like "you're absolutely right".
- **Accuracy `not_scored` ignored.** It only means Ezio found no public claims. Our data covers prices and policies, and rule 5 says it is not a reason to block.
- **Tier mapping GREEN→approve, YELLOW→hold, RED→hold.** Contradictions block through their own rule. A RED tier with no contradiction comes from tone, bias or agreement, which the rules route to review.
- **Number match tolerance ±$0.01.** Prices are exact. $79 is not $79.20, so a near miss is still a contradiction.

## Failure behavior

| Situation | Behavior | Why |
|---|---|---|
| 429 / 503 | Up to 4 attempts with bounded backoff, honoring `Retry-After`, within a 45 s budget | Transient failures shouldn't change decisions |
| Slow (8 s) | Waits (15 s per attempt) | Well inside the 60 s harness limit |
| Down, malformed, or out of retries | Our check still runs: **contradiction → BLOCK, otherwise HOLD** | A support email can wait minutes; a wrong one can't be unsent. We never fail open, and never block without evidence. |
| Accuracy `unavailable` | Contradiction → BLOCK, otherwise HOLD | We can't see legal or regulatory claims, so a person reads it |
| Bug in our checker | HOLD, with "our check failed" | Our own failure must never approve |
| Empty, oversized (>20,000 chars) or invalid request | 400 with a message; the UI prevents it before sending | Contract |

**Audit:** every decision writes the input, context, full Ezio response, every checked claim (span, stated, expected, source file and field, verdict), all checks, the decision, the reason and a timestamp to `audit_log.jsonl`. `actions.log` gets a line exactly when the decision is APPROVE, or when a reviewer approves a held draft (recorded with `approved_by`). Reviews go to `reviews.jsonl`.

**Known limits:**
- The claim checker is pattern-based. A wording it doesn't recognize produces no claim, so that sentence goes unchecked by our data, and only Ezio's scores apply to it. Figures it recognizes but can't tie to one plan are held.
- Ezio's public-knowledge coverage is whatever the mock checks.
