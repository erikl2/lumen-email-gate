# Run instructions

Requirements: Python 3.9+ (standard library only), a browser. Everything runs on `127.0.0.1`; there is no network access and nothing to install.

```bash
git clone <this repository> Approval-Gate && cd Approval-Gate
git checkout entry            # our work; branch history starts from the official starter commit fa098c4

# 1. Infrastructure tests (unchanged starter tests)
python3 test_mock.py
python3 -m unittest test_contract test_harness

# 2. Practice set + all six failure drills
python3 gate.py practice --persona customer_support

# 3. The persona page (mock + wrapper + UI). Open the printed address, then Ctrl-C to stop.
python3 gate.py serve --persona customer_support

# 4. One input through the gate, with its mock action (writes wrapper/audit_log.jsonl and wrapper/actions.log)
python3 gate.py try --persona customer_support "The Business plan is \$79 per month." \
    --context '{"plan":"Starter","customer_tenure_months":8}'

# 5. Final judging set (passphrase released at 2:30 PM PT)
python3 gate.py judge --persona customer_support --team "<team name>"
python3 gate.py results
```

## What we changed (all inside `wrapper/`)

| File | Change |
|---|---|
| `wrapper/claims.py` | **New.** Source-of-truth claim checker: number reading, plan linking, price/discount/refund/trial/SLA/eligibility checks, authority and style rules, with spans. |
| `wrapper/wrapper.py` | `check_source_of_truth()` implemented. Policy ladder combines our findings with Ezio's claims, dimensions and tier. Fails closed with our check still running when Ezio fails. Structured response: headline, consequence, review questions, claims, checks, Ezio signals. |
| `wrapper/policy.json` | Real thresholds and a one-sentence "why" for each. New knobs for source-of-truth and Ezio claim verdicts. |
| `wrapper/serve.py` | Adds `POST /review` (release or reject a **held** draft with a required reason; blocked drafts can't be released), `GET /api/audit`, `GET /api/status`, `GET /api/policy`. `/decide` is unchanged in contract. |
| `wrapper/audit.py` | Richer audit entries (claims, checks, signals, decision id) and `reviews.jsonl`. |
| `wrapper/mock_actions.py` | Reviewer-approved sends record `approved_by`. |
| `wrapper/ui/` | The persona page (`persona.html`, `app.js`, `style.css`). Offline; calls only wrapper endpoints. |

Not touched: `harness/`, `gate.py`, `mock_ezio.py`, `fixtures.json`, `personas/`, the tests. Check with `python3 gate.py digest`. No practice or final-set text appears anywhere in our code.
