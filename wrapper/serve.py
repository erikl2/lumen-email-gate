import json
import mimetypes
import os
import threading
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audit
import mock_actions
from ezio_client import BASE as EZIO_BASE
from wrapper import APPROVE, HOLD, POLICY, decide, load_source_of_truth

UI_DIR = Path(__file__).parent / "ui"
PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"

PERSONAS = {"customer_support", "paralegal", "financial_analyst"}

# Held drafts waiting for a human. The audit log is the durable copy; this is the fast path.
_pending = {}
_pending_lock = threading.Lock()


def samples(persona):
    """Practice inputs (text and context only) so a UI can offer 'simulate from a test input'."""
    path = PERSONAS_DIR / persona / "practice_set.json"
    items = json.loads(path.read_text())["items"]
    return [{"id": i["id"], "text": i["text"], "context": i["context"]} for i in items]


def ui_file(path):
    """Static files from wrapper/ui/, so a team can use plain HTML, a built SPA, or anything a browser runs."""
    name = "persona.html" if path in ("/", "") else path.lstrip("/")
    target = (UI_DIR / name).resolve()
    if UI_DIR.resolve() not in target.parents or not target.is_file():
        return None
    return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"


def ezio_status():
    try:
        with urllib.request.urlopen(EZIO_BASE + "/v1/health", timeout=2) as r:
            return "ok" if r.status == 200 else "down"
    except Exception:
        return "down"


def query_params(query):
    return dict(p.split("=", 1) for p in query.split("&") if "=" in p)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, body):
        raw = json.dumps(body, allow_nan=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_json(self):
        self.connection.settimeout(10)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 1_000_000:
            raise ValueError("Body must be 1 to 1000000 bytes")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("Body must be a JSON object")
        return payload

    def do_GET(self):
        route, _, query = self.path.partition("?")
        if route == "/health":
            return self.send_json(200, {"status": "ok"})
        if route == "/api/samples":
            persona = query_params(query).get("persona")
            if persona not in PERSONAS:
                return self.send_json(400, {"error": "Unknown persona"})
            return self.send_json(200, {"samples": samples(persona)})
        if route == "/api/status":
            return self.send_json(200, {"wrapper": "ok", "ezio": ezio_status(), "ezio_url": EZIO_BASE})
        if route == "/api/audit":
            try:
                limit = max(1, min(500, int(query_params(query).get("limit", "50"))))
            except ValueError:
                limit = 50
            return self.send_json(200, {"entries": audit.recent(limit)})
        if route == "/api/policy":
            sot = load_source_of_truth("customer_support")
            return self.send_json(200, {"thresholds": {d: v["hold_below"] for d, v in POLICY["dimension_minimums"].items()},
                                        "policy": POLICY, "price_sheet": sot.get("price_sheet", []), "policies": sot.get("policies", {})})
        served = ui_file(route)
        if served:
            raw, content_type = served
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_json(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        if self.path == "/review":
            return self.review()
        if self.path != "/decide":
            return self.send_json(404, {"error": "Unknown endpoint"})
        try:
            payload = self.read_json()
            text, persona, context = payload.get("text"), payload.get("persona"), payload.get("context", {})
            if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                raise ValueError("text must be a non-empty string of at most 20000 characters")
            if not isinstance(persona, str) or persona not in PERSONAS:
                raise ValueError("Unknown persona")
            if context is None:
                context = {}
            if not isinstance(context, dict):
                raise ValueError("context must be an object")
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Invalid request: supply text (1-20000 characters), persona, and an optional context object"})
        try:
            result = decide(text, context, persona)
            execute = result.decision == APPROVE
            if execute:
                mock_actions.run_for_persona(persona, text)
            result.action["executed"] = execute
            if result.decision == HOLD:
                with _pending_lock:
                    _pending[result.decision_id] = {"text": text, "persona": persona, "context": context}
            self.send_json(200, {**asdict(result), "action_executed": execute})
        except Exception:
            self.send_json(500, {"error": "Wrapper failed; inspect the decision implementation"})

    def review(self):
        """A human releases or rejects a HOLD_FOR_REVIEW draft. BLOCKed drafts can't be released: they must be fixed."""
        try:
            payload = self.read_json()
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Invalid request"})
        did, verdict = payload.get("decision_id"), payload.get("verdict")
        reviewer = str(payload.get("reviewer") or "").strip()[:80] or "support lead"
        note = str(payload.get("note") or "").strip()[:2000]
        if verdict not in ("approve", "reject"):
            return self.send_json(400, {"error": "verdict must be 'approve' or 'reject'"})
        if not note:
            return self.send_json(400, {"error": "A reason is required for every review decision"})
        entry = audit.find(did) if isinstance(did, str) else None
        if not entry:
            return self.send_json(404, {"error": "Unknown decision id"})
        if entry.get("decision") != HOLD:
            return self.send_json(409, {"error": f"Only held drafts can be reviewed; this one was {entry.get('decision')}. Blocked drafts must be edited and re-checked."})
        if did in audit.reviews():
            return self.send_json(409, {"error": "This draft has already been reviewed"})
        with _pending_lock:
            _pending.pop(did, None)
        executed = verdict == "approve"
        if executed:
            mock_actions.run_for_persona(entry.get("persona", "customer_support"), entry.get("input", ""), approved_by=reviewer)
        rec = audit.record_review({"decision_id": did, "verdict": verdict, "reviewer": reviewer, "note": note,
                                   "action_executed": executed, "original_decision": HOLD, "input": entry.get("input")})
        self.send_json(200, {"ok": True, "review": rec})


def main():
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "9000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Approval Gate wrapper on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
