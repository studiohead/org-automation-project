"""PLAIN ENGLISH: The ONE place that is allowed to change the client record.

Whether a person clicked "Approve" or an AI agent asked for a retry, the write goes through here, and
here the rules are enforced no matter who is asking:
  * the approval must exist and be marked APPROVED (not pending, not rejected);
  * it must be a normal "change" (exception items can never be applied);
  * the stored proposal must still match its fingerprint (what's applied == what was reviewed);
  * the CRM record must still match what the reviewer saw (otherwise nothing is written; a person re-reviews);
  * the approver must be a different party than the submitter, and never an AI agent ("independent authorization").
The CRM write carries a key tied to the approval, so repeating this call can never double-apply.
After a successful (or shadowed) CRM step, an evidence record is filed in the archive (SharePoint) by archive_record.py;
a filing problem is audited but never undoes the approval.
In WRITE_MODE=shadow the write is skipped at the CRM connector and recorded as "would have applied" instead.
"""
from org_automation.approvals.service import get_approval, mark_stale, proposal_hash
from org_automation.audit.writer import write_event
from org_automation.integrations.crm import CrmAdapter, CrmConflict, get_crm
from org_automation.storage.database import connect
from org_automation.workflows.archive_record import archive_after_apply


class ApplyError(Exception):
    pass


def apply_approved_update(approval_id: str, requested_by: str, crm: CrmAdapter | None = None) -> dict:
    item = get_approval(approval_id)
    if item is None:
        raise ApplyError("Approval not found.")
    if item["status"] != "approved":
        raise ApplyError(f"Approval is '{item['status']}', not approved.")
    if item["kind"] != "change":
        raise ApplyError("Only 'change' approvals can be applied.")
    with connect() as db:
        stored = db.execute("SELECT proposal_hash FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if proposal_hash(item["proposal"]) != stored["proposal_hash"]:
        raise ApplyError("Stored proposal no longer matches its fingerprint; refusing to act.")
    decider = item["decided_by"] or ""
    if not decider or decider == item["submitted_by"] or decider.startswith("agent:"):
        raise ApplyError("Approval lacks independent human authorization.")

    crm = crm or get_crm()
    proposal = item["proposal"]
    try:
        outcome = crm.update_record(proposal["record_id"], proposal["proposed_changes"], f"approval:{approval_id}",
                                    expected_current=proposal.get("crm_current"))
    except CrmConflict as exc:   # someone edited the record after the reviewer looked: stop, don't overwrite
        mark_stale(approval_id)
        write_event("crm_update_conflict", item["document_id"], "conflict",
                    {"approval_id": approval_id, "detail": str(exc)}, actor=requested_by)
        raise ApplyError("The CRM record changed after review; the update was NOT applied. "
                         "Submit the document again so a reviewer sees the current values.") from exc
    except Exception as exc:
        write_event("crm_update_failed", item["document_id"], "error",
                    {"approval_id": approval_id, "error": type(exc).__name__}, actor=requested_by)
        raise
    if outcome.get("shadow"):    # WRITE_MODE=shadow: nothing was written. Record what WOULD have happened, without values.
        write_event("crm_update_shadowed", item["document_id"], "would_apply" if outcome["would_apply"] else "already_current",
                    {"approval_id": approval_id, "record_id": proposal["record_id"],
                     "fields": sorted(proposal["proposed_changes"]), "approved_by": decider}, actor=requested_by)
        return {**outcome, "archive": archive_after_apply(item, approval_id, decider, requested_by, "shadow")}
    write_event("crm_update_applied", item["document_id"], "applied" if outcome["applied"] else "already_applied",
                {"approval_id": approval_id, "record_id": proposal["record_id"],
                 "fields": sorted(proposal["proposed_changes"]), "approved_by": decider}, actor=requested_by)
    result = "applied" if outcome["applied"] else "already_applied"
    return {**outcome, "archive": archive_after_apply(item, approval_id, decider, requested_by, result)}
