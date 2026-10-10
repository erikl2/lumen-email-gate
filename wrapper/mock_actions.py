"""Mock actions. They log instead of executing, so a blocked action is visible by its absence."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent) / "actions.log"
_lock = threading.Lock()


def _record(name, **args):
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": name, "args": args}
    print(f"[MOCK ACTION] {name} executed")
    with _lock, LOG.open("a") as f:
        f.write(json.dumps(line) + "\n")


def send_email(to, subject, body):
    _record("send_email", to=to, subject=subject, body=body)


def submit_memo_to_partner(memo, citations=None):
    _record("submit_memo_to_partner", memo=memo, citations=citations or [])


def publish_note_to_client(note):
    _record("publish_note_to_client", note=note)


ACTION_NAMES = {"customer_support": "send_email", "paralegal": "submit_memo_to_partner", "financial_analyst": "publish_note_to_client"}


def run_for_persona(persona, text, approved_by=None):
    """approved_by is set only when a human reviewer released a held draft (recorded in the action's args)."""
    if approved_by and persona == "customer_support":
        _record("send_email", to="customer@example.com", subject="Re: your question", body=text, approved_by=approved_by)
    elif persona == "customer_support":
        send_email("customer@example.com", "Re: your question", text)
    elif persona == "paralegal":
        submit_memo_to_partner(text)
    elif persona == "financial_analyst":
        publish_note_to_client(text)
    else:
        raise ValueError(f"unknown persona: {persona}")
