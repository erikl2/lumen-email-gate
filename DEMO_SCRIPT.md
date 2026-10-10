# Two-minute demo script

Setup before the slot: `python3 gate.py serve --persona customer_support` → open the printed address. Keep a terminal ready for `gate.py try`.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:15 | Header of **Outbound Email Gate** | "Lumen Cloud's support agents send hundreds of emails a day, and every price or refund they type is a promise in writing. Ezio scores text, but a GREEN score isn't proof the price is right. This gate sits in front of `send_email` and decides." |
| 0:15–0:35 | **Check a reply.** Plan Pro, 18 months: *"As a customer of more than 12 months, you qualify for our 15% loyalty discount on the Pro plan."* → **APPROVED** | "Three facts are checked against our price sheet and policies: the 15% rate, the 12-month rule, and *this* customer's eligibility. Green underline, evidence row, and **Email SENT**. It ran because, and only because, it was approved." |
| 0:35–1:00 | Plan Business, 10 months: *"Business is $99/mo with a first response within 24 hours."* → **BLOCKED** | "Ezio says GREEN, 100. But one of the two facts is wrong: Business gets a 4-hour response, and 24 hours is Pro. '24 hours' is struck through, the table shows what we said and what the data says, with the file and field. **Email NOT sent.** 'If this were sent' tells the lead what it would have cost. Blocked drafts can't be overridden, only fixed." |
| 1:00–1:25 | Plan Business: *"I'll make sure engineering ships the export feature next month."* → **HELD**. Tick the question, name, reason → **Approve & send** | "Not false, but not the agent's promise to make. It's held with the question the manager has to answer. Approving needs a name and a reason, and both go into the audit trail." Open the **Audit trail** tab. |
| 1:25–1:45 | **Gate simulator → Run all** | "All 16 practice drafts side by side: 8 sent, 2 held, 6 blocked. Every held or blocked card says **never reached send_email**, and `actions.log` matches exactly." |
| 1:45–2:00 | **Source of truth** tab (thresholds and their reasons) | "Thresholds come with reasons. Tone and agreement hold below 70, bias below 75, so one friendly exclamation mark is fine and repeated hype is not. If Ezio goes down, our own check still runs: it blocks what it can prove wrong and holds the rest. Final run: *(read from `results/<team>.report.md`)*. One limitation: the claim reader is pattern-based, so a wording it doesn't recognize goes unchecked by our data. The audit trail shows exactly which phrases were checked." |

**Surprise inputs:** `python3 gate.py try --persona customer_support "<judge text>" --context '{"plan":"Pro","customer_tenure_months":6}'`, or paste it into the page. Show `wrapper/audit_log.jsonl` (`tail -1 wrapper/audit_log.jsonl | python3 -m json.tool`) and `wrapper/actions.log`.

**Likely questions:**
- *Why hold, not approve, when Ezio is down?* A held email waits minutes; a wrong one can't be unsent. And we still block known-bad drafts with no Ezio at all.
- *Why not block hype?* Business rule 4 says hold, and the facts can be right.
- *Hard-coding?* `claims.py` reads the persona data files at runtime and matches patterns, not texts. Run any new wording through `gate.py try`.
