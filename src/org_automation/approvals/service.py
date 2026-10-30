"""PLAIN ENGLISH: The human review queue.

When the app wants to change something, it does NOT do it. It files a "proposal" here and waits
for a person. This file handles:
  * create_pending  – put a proposal in the queue (with a fingerprint of exactly what was proposed)
  * list_pending    – show reviewers what's waiting (list_approvals shows past decisions too)
  * decide          – record WHO approved/rejected, WHEN, and why
Safety rules enforced here: a proposal can be decided only once; the person (or robot) who submitted it can never decide it ("four-eyes"); the stored proposal is re-checked
against its fingerprint (so what's approved is exactly what was reviewed); and "exception" items
(documents that failed checks) can be rejected but never approved as-is.
"""
import hashlib
import json
import uuid
from datetime import UTC, datetime

from org_automation.storage.database import connect


class ApprovalError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def proposal_hash(proposal: dict) -> str:
    return hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_pending(document_id: str, proposal: dict, kind: str = "change", submitted_by: str = "system") -> str:
    approval_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO approvals (approval_id, document_id, kind, proposal_json, proposal_hash, status, created_at, submitted_by) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (approval_id, document_id, kind, json.dumps(proposal, sort_keys=True), proposal_hash(proposal),
             datetime.now(UTC).isoformat(), submitted_by))
    return approval_id


def _row_to_dict(r) -> dict:
    return {"approval_id": r["approval_id"], "document_id": r["document_id"], "kind": r["kind"],
            "proposal": json.loads(r["proposal_json"]), "status": r["status"], "created_at": r["created_at"],
            "submitted_by": r["submitted_by"],
            "decided_by": r["decided_by"], "decided_at": r["decided_at"], "decision_note": r["decision_note"]}


def list_pending() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at").fetchall()
    return [_row_to_dict(r) for r in rows]


def list_approvals(status: str = "all", limit: int = 50) -> list[dict]:
    """Newest first. status is pending | approved | rejected | stale | all. (Pending-only oldest-first lives in list_pending.)"""
    limit = max(1, min(limit, 200))
    with connect() as db:
        if status == "all":
            rows = db.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                              (status, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_approval(approval_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    return _row_to_dict(row) if row else None


def mark_stale(approval_id: str) -> bool:
    """The CRM record changed after a person approved this item, so the approval no longer describes reality.

    The item moves to 'stale': it can never be applied (apply_update only accepts 'approved'), and the document must be
    submitted again so a reviewer sees the CRM's current values. Returns True if the status actually changed."""
    with connect() as db:
        cur = db.execute("UPDATE approvals SET status='stale' WHERE approval_id=? AND status='approved'", (approval_id,))
        return cur.rowcount == 1


def decide(approval_id: str, reviewer: str, decision: str, note: str) -> dict:
    """Record a decision. Returns the approval as it was at decision time. Raises ApprovalError on misuse."""
    with connect() as db:
        row = db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(404, "Approval not found.")
        item = _row_to_dict(row)
        if row["status"] != "pending":
            raise ApprovalError(409, f"Already {row['status']}.")
        if proposal_hash(item["proposal"]) != row["proposal_hash"]:
            raise ApprovalError(409, "Stored proposal no longer matches its fingerprint; refusing to act.")
        if reviewer == row["submitted_by"]:
            raise ApprovalError(409, "Four-eyes rule: the submitter cannot decide their own submission.")
        if row["kind"] == "exception" and decision == "approve":
            raise ApprovalError(409, "Exception items cannot be approved; fix the source document and resubmit.")
        if decision == "approve" and not (note or "").strip():
            raise ApprovalError(422, "An approval needs a note saying what you checked.")
        new_status = "approved" if decision == "approve" else "rejected"
        cur = db.execute(  # the WHERE status='pending' makes a double-click or race safe
            "UPDATE approvals SET status=?, decided_by=?, decided_at=?, decision_note=? "
            "WHERE approval_id=? AND status='pending'",
            (new_status, reviewer, datetime.now(UTC).isoformat(), note, approval_id))
        if cur.rowcount != 1:
            raise ApprovalError(409, "Already decided.")
    item["status"] = new_status
    item["decided_by"] = reviewer
    item["decision_note"] = note
    return item
