"""Audit log: one JSON line per decision, plus one per human review. Thread-safe JSONL."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# gate.py judge/try set GATE_LOG_DIR so evaluation runs never pollute your demo audit sample.
LOG_DIR = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent)
LOG = LOG_DIR / "audit_log.jsonl"
REVIEWS = LOG_DIR / "reviews.jsonl"
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(path, entry):
    with _lock, path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def record(persona, text, context, decision):
    """Input, full mock response, every signal and piece of evidence, the decision, the reason, and a timestamp."""
    _append(LOG, {
        "timestamp": now(),
        "decision_id": getattr(decision, "decision_id", ""),
        "persona": persona,
        "input": text,
        "context": context,
        "decision": decision.decision,
        "reason": decision.reason,
        "headline": getattr(decision, "headline", ""),
        "evidence": decision.evidence,
        "claims": getattr(decision, "claims", []),
        "checks": getattr(decision, "checks", []),
        "ezio_signals": getattr(decision, "ezio_signals", {}),
        "review_questions": getattr(decision, "review_questions", []),
        "elapsed_ms": getattr(decision, "elapsed_ms", None),
        "ezio": decision.ezio,
    })


def record_review(entry):
    entry = {"timestamp": now(), **entry}
    _append(REVIEWS, entry)
    return entry


def _read(path):
    if not path.exists():
        return []
    out = []
    with _lock:
        lines = path.read_text().splitlines()
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def find(decision_id):
    for e in reversed(_read(LOG)):
        if e.get("decision_id") == decision_id:
            return e
    return None


def reviews():
    return {r.get("decision_id"): r for r in _read(REVIEWS)}


def recent(limit=50):
    revs = reviews()
    rows = []
    for e in reversed(_read(LOG)[-limit:]):
        rows.append({k: e.get(k) for k in ("timestamp", "decision_id", "input", "context", "decision", "reason", "headline", "elapsed_ms")}
                    | {"action_executed": e.get("decision") == "APPROVE", "review": revs.get(e.get("decision_id"))})
    return rows
