"""PLAIN ENGLISH: The assembly line for one incoming document.

Steps, in order:
  1. Claim the event ID  -> if we've seen it before, stop (no double-processing).
  2. Read the document   -> pull out client ID, date, email (demo reader or Claude).
  3. Fact-check          -> rules in validation/rules.py, plus "does this client exist in the CRM?"
  4. File for a human    -> clean results become a "change" proposal (showing CURRENT vs PROPOSED values);
                            anything suspicious becomes an "exception" item. NOTHING is silently dropped.
  5. Write to the audit diary.
Crucially, this assembly line NEVER changes the CRM. Only a person's approval (review_decision.py) can.
If something crashes midway, the claim is released so a retry can run, and the crash is logged.
"""
from org_automation.ai.extraction import as_pages, get_extractor
from org_automation.approvals.service import create_pending
from org_automation.audit.writer import write_event
from org_automation.core.idempotency import claim_event, finish_event, previous_status, release_event
from org_automation.documents.schemas import ExtractedDocument, ValidationIssue, WorkflowResult
from org_automation.integrations.crm import CrmAdapter, CrmAmbiguous, get_crm
from org_automation.validation.rules import validate


def run_checks(extracted: ExtractedDocument, pages: list[str], crm: CrmAdapter) -> tuple[list[ValidationIssue], dict | None]:
    """All the fact-checking in one place (also used by the accuracy report). Returns (issues, current CRM record)."""
    issues = validate(extracted, pages)
    current = None
    if extracted.client_id.value:
        try:
            current = crm.get_record(extracted.client_id.value)
        except CrmAmbiguous:
            issues.append(ValidationIssue(field="client_id", code="ambiguous_client",
                                          message="More than one CRM record has this client number."))
            return issues, None
        if current is None:
            issues.append(ValidationIssue(field="client_id", code="unknown_client",
                                          message="No such client in the CRM."))
    return issues, current


def build_proposal(extracted: ExtractedDocument, issues: list[ValidationIssue], current: dict | None) -> tuple[str, str, dict]:
    """Turn checked results into a review item. Returns (kind, workflow status, proposal). Shared by the
    fixed assembly line AND the AI agent's submit_for_review tool, so both produce identical items."""
    if issues:
        return "exception", "needs_exception_review", {"extracted": extracted.model_dump(mode="json"),
                                                         "issues": [i.model_dump() for i in issues]}
    return "change", "needs_review", {
        "record_id": extracted.client_id.value,
        "extracted": extracted.model_dump(mode="json"),
        "crm_current": {k: (current or {}).get(k) for k in ("email", "effective_date")},
        "proposed_changes": {"email": extracted.email.value, "effective_date": extracted.effective_date.value}}


def process_document(document_id: str, document_type: str, text_or_pages: str | list[str], event_id: str,
                     actor: str = "system", extractor=None, crm: CrmAdapter | None = None) -> WorkflowResult:
    key, won = claim_event("api", event_id, document_id)
    if not won:
        audit_id = write_event("duplicate_event", document_id, "ignored", {"event_id": event_id}, actor)
        return WorkflowResult(document_id=document_id, status="duplicate_ignored", audit_event_id=audit_id,
                              message=f"Event already processed (earlier status: {previous_status(key)}); no new action taken.")
    try:
        extractor = extractor or get_extractor()
        crm = crm or get_crm()
        pages = as_pages(text_or_pages)
        extracted = extractor.extract(document_id, document_type, pages)
        issues, current = run_checks(extracted, pages, crm)

        kind, status, proposal = build_proposal(extracted, issues, current)
        approval_id = create_pending(document_id, proposal, kind, submitted_by=actor)
        audit_id = write_event("document_processed", document_id, status,
                               {"event_id": event_id, "issue_codes": sorted({i.code for i in issues}),
                                "approval_id": approval_id, "extractor": type(extractor).__name__}, actor)
        finish_event(key, status)
        return WorkflowResult(document_id=document_id, status=status, extracted=extracted, issues=issues,
                              approval_id=approval_id, proposed_action=f"review:{approval_id}",
                              audit_event_id=audit_id,
                              message="No external write performed. Human review is required before downstream changes.")
    except Exception as exc:
        release_event(key)  # let a retry try again
        write_event("processing_error", document_id, "error", {"event_id": event_id, "error": type(exc).__name__}, actor)
        raise
