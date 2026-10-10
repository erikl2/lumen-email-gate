"""Approval Gate wrapper for the customer-support persona (Lumen Cloud).

Flow: check the draft against our own data (works even when Ezio is down) -> call Ezio over HTTP -> read its signals
-> apply policy.json -> decision + plain-language reason + evidence -> audit log -> mock action only if APPROVE.

Decision ladder (strictest wins):
  BLOCK            a statement contradicts price_sheet.csv / policies.json, or Ezio marks a claim contradicted
  HOLD_FOR_REVIEW  a promise/exception/guarantee/hype, a weak Ezio dimension, an unverifiable claim, or a missing signal
  APPROVE          everything checkable matched, and Ezio saw nothing wrong (a draft with no checkable claims is fine)

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan."
Score yourself: python3 evaluate.py customer_support
"""
import csv
import json
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import claims as claimcheck
import mock_actions
from ezio_client import EzioError, analyze

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}
DIMENSIONS = ("tone", "bias", "agreement")


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)
    label: str = ""


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict
    decision_id: str = ""
    headline: str = ""
    consequence: str = ""
    review_questions: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    ezio_signals: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    elapsed_ms: int = 0


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in DIMENSIONS]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        claims = [c for c in resp.get("claims", []) if isinstance(c, dict) and c.get("verdict") in ("supported", "contradicted", "unverifiable")]
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=claims, label=str(verdict.get("label", "")),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise EzioError("malformed mock response", resp) from e


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


def check_source_of_truth(text, context, sot):
    """Independent signal: every price, discount, refund, trial, response-time, eligibility, authority and style
    statement in the draft, checked against price_sheet.csv + policies.json (see claims.py).
    Returns (findings, claims): findings are the BLOCK/HOLD items, claims are every checked statement incl. verified ones."""
    checked = claimcheck.check(text, context, sot)
    findings = [Finding(BLOCK if c.verdict == claimcheck.CONTRADICTED else HOLD, c.message)
                for c in checked if c.verdict != claimcheck.VERIFIED]
    return findings, checked


# ---------------------------------------------------------------------------------------------------------------
# Helpers for Ezio's claims

def _words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower())) - {"the", "a", "an", "of", "to", "in", "and", "is", "are", "be", "only", "can", "your", "you"}


def locate(text, claim_text):
    """Find the sentence of the draft that an Ezio claim refers to (best word overlap), for highlighting."""
    best, best_score = None, 0.0
    target = _words(claim_text)
    if not target:
        return None
    for s in claimcheck.sentences(text):
        overlap = len(target & _words(s.text)) / len(target)
        if overlap > best_score:
            best, best_score = s, overlap
    if best and best_score >= 0.4:
        lead = len(best.text) - len(best.text.lstrip())
        return [best.start + lead, best.end]
    return None


def _publisher(c):
    src = c.get("source") or {}
    return src.get("publisher") if isinstance(src, dict) else str(src)


# ---------------------------------------------------------------------------------------------------------------
# Policy

class Ladder:
    """Collects reasons with a level; the strictest level is the decision."""

    def __init__(self):
        self.decision = APPROVE
        self.items = []  # (level, reason, question, consequence)

    def raise_to(self, level, reason, question=None, consequence=None):
        self.items.append((level, reason, question, consequence))
        if RANK[level] > RANK[self.decision]:
            self.decision = level

    def top(self, level):
        return [i for i in self.items if i[0] == level]


def apply_policy(signals, findings, policy, checked=(), text="", ezio_error=None):
    """Combine our own findings with Ezio's signals. signals is None when Ezio failed (down, malformed, timeout)."""
    lad = Ladder()
    th = {d: policy["dimension_minimums"][d]["hold_below"] for d in DIMENSIONS}

    # 1. Our source of truth (always runs; does not depend on Ezio). One reason per rule, however often it trips.
    seen = set()
    for c in checked:
        if c.verdict != claimcheck.VERIFIED and (c.verdict, c.label) in seen:
            continue
        seen.add((c.verdict, c.label))
        if c.verdict == claimcheck.CONTRADICTED:
            lad.raise_to(policy["source_of_truth_contradiction"], c.message,
                         consequence=f"the customer would be told {c.stated} ({c.label}) when company data says {c.expected}")
        elif c.verdict == claimcheck.REVIEW:
            lad.raise_to(policy["source_of_truth_needs_review"], c.message, question=review_question(c))

    # 2. Ezio unavailable entirely: fail closed to a human unless our own data already proves the draft wrong.
    if signals is None:
        lad.raise_to(policy["on_ezio_failure"],
                     f"Ezio could not be reached ({ezio_error}); our price/policy checks still ran, but tone and public-claim checks did not.",
                     question="Ezio's fact-check and tone scoring were unavailable. Read the draft for tone and for any statement about laws, regulations or third parties before sending.",
                     consequence="an unscored email could go out")
        return lad

    # 3. Ezio's per-claim fact-check (public knowledge our files don't cover).
    if signals.accuracy_status == "scored":
        for c in signals.claims:
            pub = _publisher(c)
            if c["verdict"] == "contradicted":
                lad.raise_to(policy["ezio_contradicted_claim"], f"Ezio's fact-check contradicts: \"{c.get('text')}\"" + (f" (source: {pub})." if pub else "."),
                             consequence=f"the customer would receive a statement public sources contradict (\"{c.get('text')}\")")
            elif c["verdict"] == "unverifiable":
                lad.raise_to(policy["ezio_unverifiable_claim"], f"Ezio could not verify: \"{c.get('text')}\".",
                             question=f"Ezio could not verify \"{c.get('text')}\". Can you confirm it from a trusted source, or remove it?")
    elif signals.accuracy_status == "unavailable" and policy["when_accuracy_unavailable"] != "ignore":
        lad.raise_to(policy["when_accuracy_unavailable"],
                     "Ezio's fact-check was unavailable, so public-knowledge claims (laws, regulations) could not be checked.",
                     question="The fact-check was unavailable. Does the email state any law, regulation or third-party fact that needs checking?",
                     consequence="an unchecked legal or regulatory statement could reach the customer")
    elif signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        lad.raise_to(policy["when_accuracy_not_scored"], "Ezio extracted no claims to check (accuracy not scored).")

    # 4. Individual dimensions: a GREEN overall tier can hide one weak dimension.
    names = {"tone": "Tone", "bias": "Bias (one-sided framing)", "agreement": "Agreement (flattery / over-agreeing)"}
    for d in DIMENSIONS:
        score = getattr(signals, d)
        if score < th[d]:
            lad.raise_to(HOLD, f"{names[d]} scored {score:g}, below our {th[d]:g} threshold.",
                         question=f"Ezio scored {d} at {score:g} (threshold {th[d]:g}). Is this wording acceptable to send to a customer? If not, reject and rewrite.",
                         consequence="the customer would receive an email that reads as " + {"tone": "pushy or promotional", "bias": "one-sided", "agreement": "flattering or over-agreeing"}[d])

    # 5. Overall tier, as a backstop (never the only reason to approve).
    tier_level = policy["tier_decisions"][signals.tier]
    if tier_level != APPROVE:
        lad.raise_to(tier_level, f"Ezio rates the draft {signals.tier} ({signals.score:g}): {signals.summary}",
                     question=f"Ezio rated this {signals.tier}: {signals.summary} Review the flagged issues before sending.")
    return lad


def review_question(c):
    k = c.kind
    if k == "commitment":
        return "Has product/engineering confirmed this commitment (feature and date) in writing? If not, reject and rewrite without the promise."
    if k == "exception":
        return f"Is a manager authorizing this exception (\"{c.stated}\")? Record who approved it in the note."
    if k == "tone" and "Flattery" in c.label:
        return "The email agrees with or flatters the customer. Is what it agrees with actually true? Rewrite as a neutral, factual reply if not."
    if k == "tone":
        return "Remove the hype, guarantees and exclamation marks (highlighted in the draft), or approve only if a manager accepts this wording."
    if k == "discount_eligibility":
        return "Confirm the customer's plan and tenure meet the loyalty policy (Pro or Business, 12+ months) before sending."
    if k == "price":
        return f"Clarify the price: {c.expected}."
    if k == "price_saving":
        return f"Check the arithmetic: {c.message}"
    return f"Check: {c.message}"


# ---------------------------------------------------------------------------------------------------------------
# Presenting the decision

CHECK_GROUPS = [
    ("Prices & plan facts", ("price", "price_enterprise", "price_saving", "response_hours", "trial_days", "trial_card")),
    ("Discounts & refunds", ("discount_loyalty_pct", "discount_annual_pct", "discount_pct", "stacking", "loyalty_tenure", "refund_window", "refund_scope")),
    ("Discount eligibility (customer context)", ("discount_eligibility",)),
    ("Commitments & exceptions", ("commitment", "exception")),
    ("Hype, guarantees & flattery", ("tone",)),
]


def build_checks(checked, signals, ezio_error, th):
    out = []
    for name, kinds in CHECK_GROUPS:
        group = [c for c in checked if c.kind in kinds]
        if not group:
            out.append({"name": name, "status": "skip", "detail": "Nothing of this kind in the draft."})
            continue
        bad = [c for c in group if c.verdict == claimcheck.CONTRADICTED]
        rev = [c for c in group if c.verdict == claimcheck.REVIEW]
        ok = [c for c in group if c.verdict == claimcheck.VERIFIED]
        status = "fail" if bad else "warn" if rev else "pass"
        detail = "; ".join(c.message for c in (bad or rev)[:2]) if (bad or rev) else f"{len(ok)} statement(s) verified against company data."
        out.append({"name": name, "status": status, "detail": detail})
    if signals is None:
        out.append({"name": "Ezio fact-check", "status": "fail", "detail": f"Ezio unavailable: {ezio_error}. Our own checks above still ran."})
        for d in DIMENSIONS:
            out.append({"name": f"Ezio {d}", "status": "skip", "detail": "Not scored (Ezio unavailable)."})
        return out
    if signals.accuracy_status == "scored":
        contra = [c for c in signals.claims if c["verdict"] == "contradicted"]
        unver = [c for c in signals.claims if c["verdict"] == "unverifiable"]
        status = "fail" if contra else "warn" if unver else "pass"
        detail = f"{signals.claims_verified}/{signals.claims_total} supported" + (f", {len(contra)} contradicted" if contra else "") + (f", {len(unver)} unverifiable" if unver else "") + "."
    elif signals.accuracy_status == "not_scored":
        status, detail = "skip", "No public-knowledge claims to check (not scored). Not a reason to hold: prices and policies are checked against our data above."
    else:
        status, detail = "warn", "Fact-check service unavailable: public-knowledge claims could not be checked."
    out.append({"name": "Ezio fact-check", "status": status, "detail": detail})
    for d in DIMENSIONS:
        score = getattr(signals, d)
        out.append({"name": f"Ezio {d}", "status": "warn" if score < th[d] else "pass",
                    "detail": f"{score:g} (hold below {th[d]:g})"})
    out.append({"name": "Ezio overall tier", "status": {"GREEN": "pass", "YELLOW": "warn", "RED": "fail"}[signals.tier],
                "detail": f"{signals.tier} {signals.score:g}: {signals.summary}"})
    return out


def ezio_signals_dict(signals, response, ezio_error, th):
    base = {"thresholds": th}
    if signals is None:
        return {**base, "status": "down", "tier": None, "score": None, "label": None, "summary": None, "accuracy_status": None,
                "claims_total": 0, "claims_verified": 0, "claims_contradicted": 0, "tone": None, "bias": None, "agreement": None,
                "claims": [], "error": ezio_error}
    return {**base, "status": "degraded" if signals.accuracy_status == "unavailable" else "ok", "tier": signals.tier,
            "score": signals.score, "label": signals.label, "summary": signals.summary, "accuracy_status": signals.accuracy_status,
            "claims_total": signals.claims_total, "claims_verified": signals.claims_verified,
            "claims_contradicted": signals.claims_contradicted, "tone": signals.tone, "bias": signals.bias,
            "agreement": signals.agreement, "claims": signals.claims, "error": None,
            "findings": [f for f in (response or {}).get("findings", []) if isinstance(f, dict)]}


def compose(text, context, lad, checked, signals, response, ezio_error, policy, persona):
    th = {d: policy["dimension_minimums"][d]["hold_below"] for d in DIMENSIONS}
    decision = lad.decision
    claim_dicts = [c.to_dict() for c in checked]
    if signals is not None:
        for c in signals.claims:
            claim_dicts.append({
                "kind": "ezio_claim", "label": "Ezio fact-check", "span": locate(text, c.get("text", "")), "text": c.get("text"),
                "verdict": {"supported": "verified", "contradicted": "contradicted", "unverifiable": "review"}[c["verdict"]],
                "message": f"Ezio: {c['verdict']}" + (f" (source: {_publisher(c)})" if _publisher(c) else ""),
                "plan": None, "stated": c.get("text"), "expected": c["verdict"], "source": _publisher(c),
                "level": {"contradicted": BLOCK, "unverifiable": HOLD}.get(c["verdict"]),
                "url": (c.get("source") or {}).get("url") if isinstance(c.get("source"), dict) else None,
            })
    claim_dicts.sort(key=lambda c: (c["span"] or [10 ** 9])[0])
    for i, c in enumerate(claim_dicts, 1):
        c["id"] = f"c{i}"

    verified = [c for c in checked if c.verdict == claimcheck.VERIFIED]
    blockers = lad.top(BLOCK)
    holds = lad.top(HOLD)
    if decision == BLOCK:
        headline = "Blocked: " + _first_sentence(blockers[0][1])
        reasons = [b[1] for b in blockers]
        cons = [b[3] for b in blockers if b[3]]
        consequence = ("send_email did NOT run. If this had been sent, " + "; and ".join(cons[:2]) + ". We would have to honor it or retract it in writing."
                       if cons else "send_email did NOT run. The draft must be corrected and re-checked.")
        questions = ["Correct the flagged statement(s) to match company data, then re-check. Blocked drafts cannot be approved as written."]
    elif decision == HOLD:
        headline = "Held for review: " + _first_sentence(holds[0][1])
        reasons = [h[1] for h in holds]
        cons = [h[3] for h in holds if h[3]]
        consequence = ("send_email did NOT run; the draft waits for a reviewer. If approved as written, " + "; and ".join(cons[:2]) + "."
                       if cons else "send_email did NOT run; the draft waits for a reviewer. Approving sends it exactly as written.")
        questions = list(dict.fromkeys(h[2] for h in holds if h[2])) or ["Read the flagged wording and decide whether it can be sent as written."]
    else:
        if verified:
            facts = ", ".join(sorted({c.label for c in verified}))
            headline = f"Approved: {len(verified)} statement(s) match company data ({facts})."
        elif signals is not None and signals.claims:
            headline = f"Approved: Ezio's fact-check supports all {len(signals.claims)} claim(s); no conflicts with company data."
        else:
            headline = "Approved: no prices, policies or promises to check, and Ezio saw no tone, bias or agreement problems."
        reasons = [headline[len("Approved: "):]]
        consequence = "send_email ran: the email was sent to customer@example.com as written."
        questions = []

    reason = (" ".join(reasons[:3]) if decision != APPROVE else reasons[0])
    if signals is not None:
        reason += f" [Ezio {signals.tier} {signals.score:g}, accuracy {signals.accuracy_status.replace('_', ' ')}]"
    else:
        reason += " [Ezio unavailable]"

    evidence = []
    for c in checked:
        mark = {"verified": "OK", "contradicted": "CONTRADICTED", "review": "REVIEW"}[c.verdict]
        evidence.append(f"{mark}: \"{c.text}\" — {c.message}" + (f" [{c.source}]" if c.source else ""))
    if signals is not None:
        for c in signals.claims:
            evidence.append(f"Ezio claim {c['verdict'].upper()}: \"{c.get('text')}\"" + (f" [{_publisher(c)}]" if _publisher(c) else ""))
        evidence.append(f"Ezio: {signals.tier} {signals.score:g} ({signals.summary}) accuracy={signals.accuracy_status}, "
                        f"tone={signals.tone:g}, bias={signals.bias:g}, agreement={signals.agreement:g} (hold below {th['tone']:g}/{th['bias']:g}/{th['agreement']:g}).")
    else:
        evidence.append(f"Ezio unavailable: {ezio_error}. Decision based on our own price/policy checks, failing closed to review.")
    if not checked:
        evidence.append("No price, discount, refund, trial, response-time, eligibility, promise or style statements found in the draft.")

    return dict(
        decision=decision, reason=reason, evidence=evidence, headline=headline, consequence=consequence,
        review_questions=questions, claims=claim_dicts, checks=build_checks(checked, signals, ezio_error, th),
        ezio_signals=ezio_signals_dict(signals, response, ezio_error, th),
    )


def _first_sentence(s):
    return s if len(s) <= 160 else s[:157].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------------------------------------------------------

def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    started = time.monotonic()
    context = context if isinstance(context, dict) else {}
    decision_id = "d-" + uuid.uuid4().hex[:10]

    # Our own check first: it needs no network, so it still protects the customer when Ezio is down.
    try:
        findings, checked = check_source_of_truth(text, context, load_source_of_truth(persona))
    except Exception as e:  # a bug in our checker must never approve: fail closed to a human
        checked = [claimcheck.Claim("internal", "Source-of-truth check", None, "", claimcheck.REVIEW,
                                    f"Our price/policy check failed ({type(e).__name__}); a person must check the facts.")]
        findings = [Finding(HOLD, checked[0].message)]

    response, signals, ezio_error = None, None, None
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        ezio_error = str(e)
        response = {"error": ezio_error, "response": e.response}

    lad = apply_policy(signals, findings, POLICY, checked, text, ezio_error)
    out = compose(text, context, lad, checked, signals, response, ezio_error, POLICY, persona)
    result = Decision(
        out["decision"], out["reason"], out["evidence"], response if response is not None else {},
        decision_id=decision_id, headline=out["headline"], consequence=out["consequence"],
        review_questions=out["review_questions"], claims=out["claims"], checks=out["checks"],
        ezio_signals=out["ezio_signals"],
        action={"name": mock_actions.ACTION_NAMES.get(persona, "action"), "to": "customer@example.com",
                "subject": "Re: your question", "executed": False},
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    for line in result.evidence:
        print(f"  - {line}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
