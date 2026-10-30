"""PLAIN ENGLISH: After a person approves a change and the CRM step is done, file a copy of the decision in SharePoint.

The filed record says what was proposed, who approved it, when, and their note: evidence that lives outside this app.
Rules, in the same spirit as the CRM step:
  * It runs only AFTER a human approval and only from the one place that applies approvals (apply_update.py).
    The AI agent has no tool for it and cannot trigger it any other way.
  * A real archive (ARCHIVE_PROVIDER=sharepoint) follows the write gate: unless WRITE_MODE=enabled it files nothing
    and the audit log says "would have filed". The local mock archive always files (it is just a table).
  * The file name comes from the approval, so a retry finds the earlier copy instead of making a second one.
  * A filing problem NEVER undoes or blocks the approval or the CRM change; it is audited as `archive_failed`
    (error type only) so someone can retry. The CRM is the system of record; the archive is the evidence copy.
  * The record is redacted text, and the audit log keeps only the opaque SharePoint item id, never the content.
"""
import json
from datetime import UTC, datetime

from org_automation.audit.writer import write_event
from org_automation.core.redaction import scrub
from org_automation.core.settings import settings
from org_automation.integrations.microsoft_graph import get_archive


def build_record(item: dict, approval_id: str, decider: str, crm_result: str) -> bytes:
    """The evidence record. `scrub` blanks anything that looks like an SSN or account number before it leaves the app."""
    proposal = item["proposal"]
    record = {
        "approval_id": approval_id,
        "document_id": item["document_id"],
        "client_record_id": proposal.get("record_id"),
        "proposed_changes": proposal.get("proposed_changes"),
        "crm_before": proposal.get("crm_current"),
        "submitted_by": item["submitted_by"],
        "approved_by": decider,
        "approval_note": item.get("decision_note"),
        "crm_result": crm_result,                       # applied | already_applied | shadow
        "filed_at": datetime.now(UTC).isoformat(),
    }
    return json.dumps(scrub(record), indent=2, sort_keys=True).encode("utf-8")


def archive_after_apply(item: dict, approval_id: str, decider: str, requested_by: str, crm_result: str) -> dict:
    """File the record if an archive is configured. Never raises: returns a small, safe status dict."""
    provider = settings.archive_provider
    if provider == "none":
        return {"status": "disabled"}
    document_id = item["document_id"]
    if provider == "sharepoint" and settings.write_mode != "enabled":
        write_event("archive_shadowed", document_id, "would_file", {"approval_id": approval_id, "provider": provider},
                    actor=requested_by)
        return {"status": "shadow"}
    try:
        archive = get_archive()
        receipt = archive.archive_record(document_id, approval_id, build_record(item, approval_id, decider, crm_result))
    except Exception as exc:  # noqa: BLE001  (never block or undo an approval over filing; keep the error type only)
        write_event("archive_failed", document_id, "error",
                    {"approval_id": approval_id, "provider": provider, "error": type(exc).__name__}, actor=requested_by)
        return {"status": "failed", "error": type(exc).__name__}
    write_event("document_archived", document_id, "already_filed" if receipt.already_existed else "filed",
                {"approval_id": approval_id, "provider": provider, "drive_item_id": receipt.drive_item_id}, actor=requested_by)
    return {"status": "already_filed" if receipt.already_existed else "filed", "drive_item_id": receipt.drive_item_id}
