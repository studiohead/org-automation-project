"""PLAIN ENGLISH: The "have I seen this before?" stamp.

Systems like DocuSign and Power Automate sometimes deliver the same message twice (retries are
normal). If we processed both, a client could get double updates. Each incoming event gets a
unique fingerprint; the FIRST caller to "claim" it wins and everyone else is told "already handled".
Claiming is a single atomic database step, so two simultaneous requests can't both win.
"""
import hashlib
from datetime import UTC, datetime

from org_automation.storage.database import connect


def idempotency_key(provider: str, event_id: str) -> str:
    return hashlib.sha256(f"{provider}:{event_id}".encode()).hexdigest()


def claim_event(provider: str, event_id: str, document_id: str) -> tuple[str, bool]:
    """Try to claim an event. Returns (key, True) if we won, (key, False) if it was already claimed."""
    key = idempotency_key(provider, event_id)
    with connect() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO processed_events(idempotency_key, event_id, document_id, status, created_at) "
            "VALUES (?, ?, ?, 'processing', ?)",
            (key, event_id, document_id, datetime.now(UTC).isoformat()),
        )
        return key, cur.rowcount == 1


def finish_event(key: str, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE processed_events SET status=? WHERE idempotency_key=?", (status, key))


def release_event(key: str) -> None:
    """Give the claim back after a crash so a retry is allowed to try again."""
    with connect() as db:
        db.execute("DELETE FROM processed_events WHERE idempotency_key=?", (key,))


def previous_status(key: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT status FROM processed_events WHERE idempotency_key=?", (key,)).fetchone()
    return row["status"] if row else None
