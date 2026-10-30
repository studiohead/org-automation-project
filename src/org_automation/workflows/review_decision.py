"""PLAIN ENGLISH: What happens when a person clicks Approve or Reject.

  * The decision is recorded with the reviewer's name and the time.
  * If REJECTED: nothing else happens.
  * If APPROVED: the change is applied through `apply_update.py` — the single, rule-enforcing gate.
Both steps are written to the audit diary. If the CRM is down at that moment, the approval stays recorded and
the apply can be retried later (e.g. by an agent "resume" run) — safe, because the write is idempotent.
"""
from org_automation.approvals.service import decide
from org_automation.audit.writer import write_event
from org_automation.integrations.crm import CrmAdapter
from org_automation.workflows.apply_update import apply_approved_update


def review(approval_id: str, reviewer: str, decision: str, note: str, crm: CrmAdapter | None = None) -> dict:
    item = decide(approval_id, reviewer, decision, note)
    write_event("approval_decided", item["document_id"], item["status"],
                {"approval_id": approval_id, "kind": item["kind"], "note": note}, actor=reviewer)
    result = {"approval_id": approval_id, "status": item["status"], "crm_update": None}
    if decision == "approve":
        result["crm_update"] = apply_approved_update(approval_id, requested_by=reviewer, crm=crm)
    return result
