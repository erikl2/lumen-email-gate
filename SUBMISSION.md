# Devpost submission: Approval Gate track

**Project name:** Lumen Cloud Outbound Email Gate
**Team:** `<team name>` · Members: `<names>`
**Track:** Approval Gate · **Persona:** Customer support lead · **Gated action:** `send_email(to, subject, body)`

## One-liner
A trust score doesn't decide, so this wrapper does: it checks every price, discount, refund, trial and response-time claim in an agent's draft against the company's own data, combines that with Ezio's per-claim fact-check and per-dimension scores, and only then lets `send_email` run.

## What it does
- **BLOCK** when a statement contradicts `price_sheet.csv` / `policies.json`, or when Ezio contradicts a claim. Examples: "$79" for a $99 plan, a 30% loyalty discount, stacked discounts, a year-long refund on an annual plan, a trial on Starter, a 24-hour SLA on Business, a loyalty discount for a 6-month customer, a GDPR misstatement.
- **HOLD_FOR_REVIEW** for things that aren't provably false but aren't the agent's call: feature promises and delivery dates, exceptions and goodwill credits, guarantees, hype, flattery, a weak tone/bias/agreement score behind a GREEN tier, or an unverifiable claim.
- **APPROVE** when everything checkable matches. That includes emails with nothing to check, because a missing accuracy score is not a reason to block.
- Reads figures written any way: "nineteen dollars", "$0.099 thousand", "two weeks", "a fifth off", "24hrs". Handles several facts in one sentence, linking each figure to the plan in its own clause.
- **Degrades safely:** our check needs no network, so if Ezio is down or malformed, provably wrong drafts are still **blocked** and everything else is **held**. It never fails open.

## The persona page
`gate.py serve` → **Outbound Email Gate**:
- **Compose:** customer plan and tenure, the draft, and a character limit.
- **Decision banner:** icon, word and color together.
- **The draft:** every checked phrase is underlined (verified ✓, needs review ⚠, contradicted ✕).
- **Evidence table:** what the email says vs. the source of truth, with file and field.
- **"If this email were sent":** the consequence of sending.
- **Action status:** "Email SENT" or "Email NOT sent".
- **Ezio signals:** dimension bars with threshold marks.
- **Review step:** checklist, reviewer and a required reason. Blocked drafts can't be overridden.
- **Gate simulator:** runs all sample drafts side by side and shows which never reached `send_email`.
- **Audit trail:** recent decisions and their review outcomes.
- **Source of truth:** the price sheet, policies, and each threshold with its reason.

Screenshots are in `docs/screenshots/`.

## Results
- Practice set: **16/16 exact, 8/8 caught, 0/8 false blocks**. Failure drills: **15/15** (down, rate-limited, server error, malformed, unavailable, slow).
- Our own 74 new drafts, written from the business rules (not the practice texts): **74/74**, 42/42 bad caught, 0/32 false blocks.
- Final run: see `results/<team>.json` and `results/<team>.report.md` (submitted unchanged).

## How we built it
- Python 3.9 standard library only.
- Plain HTML/CSS/JS for the page, fully offline.
- The official starter is unchanged outside `wrapper/` (tool digest `939643512c57edb1`).
- Decision policy: [`DECISION_POLICY.md`](DECISION_POLICY.md). Run instructions: [`RUN.md`](RUN.md).

## AI tools used
Built during the event window with **Claude Code (Claude Opus 5.5)**, which wrote code, tests and docs under our direction. We verified everything:
- the starter's own tests;
- `gate.py practice` after every change;
- two batches of novel, self-labeled drafts run through the real mock;
- live browser tests of the page;
- a scan confirming that no practice text is embedded in our code.

## Best use of Ezio (optional note)
We use Ezio for what our files can't see: per-claim verdicts (a contradicted public-law claim → BLOCK; a supported one → no second guessing) and per-dimension scores (tone, bias and agreement thresholds catch hype behind a GREEN tier). We use our own data for what Ezio can't see: private prices and policies. Every Ezio signal shown in the UI is labeled with what it does and doesn't prove.

## Limitation
The claim reader is pattern-based: a wording it doesn't recognize produces no claim, so that sentence is checked only by Ezio, not by our data. Recognized but ambiguous figures are held, and the audit trail and highlighted draft show exactly which phrases were checked.
