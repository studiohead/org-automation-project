"""PLAIN ENGLISH: The permanent diary — and a way to prove nobody edited it.

Every important thing the app does (received a document, flagged a problem, a person approved a
change, a webhook arrived) is written down here with WHO did it and WHEN.

The clever part: each entry also stores a fingerprint (hash) that mixes in the previous entry's
fingerprint — like pages of a book numbered in a way that exposes any torn-out or rewritten page.
`verify_chain()` re-checks the whole diary; if anyone altered or deleted an old entry, it says so.
(This is "tamper-EVIDENT", not tamper-proof: production should also ship the log to write-once storage.)
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime

from org_automation.core.redaction import redact_text, scrub
from org_automation.storage.database import connect

GENESIS = "0" * 64  # the "previous fingerprint" for the very first entry


def _fingerprint(prev_hash: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def _payload(event_id, timestamp, event_type, document_id, actor, outcome, details_json) -> dict:
    return {"event_id": event_id, "timestamp": timestamp, "event_type": event_type,
            "document_id": document_id, "actor": actor, "outcome": outcome, "details": details_json}


def write_event(event_type: str, document_id: str, outcome: str, details: dict, actor: str = "system") -> str:
    event_id = str(uuid.uuid4())
    timestamp = datetime.now(UTC).isoformat()
    document_id = redact_text(document_id)[:200]   # an AI-supplied "document id" is still free text: never log raw digits
    details_json = json.dumps(scrub(details), sort_keys=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")  # lock the diary so two writers can't grab the same "previous" entry
        row = db.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = row["hash"] if row else GENESIS
        digest = _fingerprint(prev_hash, _payload(event_id, timestamp, event_type, document_id, actor, outcome, details_json))
        db.execute(
            "INSERT INTO audit_events (event_id, timestamp, event_type, document_id, actor, outcome, details_json, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, timestamp, event_type, document_id, actor, outcome, details_json, prev_hash, digest),
        )
    return event_id


def verify_chain() -> dict:
    """Re-walk the whole diary. Returns {"ok": True, "events": N} or says where it first breaks."""
    with connect() as db:
        rows = db.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
    prev = GENESIS
    for r in rows:
        expected = _fingerprint(prev, _payload(r["event_id"], r["timestamp"], r["event_type"], r["document_id"],
                                               r["actor"], r["outcome"], r["details_json"]))
        if r["prev_hash"] != prev or r["hash"] != expected:
            return {"ok": False, "events": len(rows), "first_bad_seq": r["seq"]}
        prev = r["hash"]
    return {"ok": True, "events": len(rows)}


def list_events(limit: int = 50, document_id: str | None = None) -> list[dict]:
    """The newest diary entries first (for the audit screen). Details were already redacted when written."""
    limit = max(1, min(limit, 200))
    sql = "SELECT seq, event_id, timestamp, event_type, document_id, actor, outcome, details_json FROM audit_events"
    args: list = []
    if document_id:
        sql += " WHERE document_id=?"
        args.append(document_id)
    with connect() as db:
        rows = db.execute(sql + " ORDER BY seq DESC LIMIT ?", (*args, limit)).fetchall()
    return [{"seq": r["seq"], "timestamp": r["timestamp"], "event_type": r["event_type"],
             "document_id": r["document_id"], "actor": r["actor"], "outcome": r["outcome"],
             "details": json.loads(r["details_json"])} for r in rows]
