"""PLAIN ENGLISH: Tests of the assembly line — duplicates ignored, problems go to a human queue
(not the trash), a crash doesn't permanently block a retry, and the audit diary resists tampering."""
import sqlite3

import pytest

from org_automation.approvals.service import list_pending
from org_automation.audit.writer import verify_chain
from org_automation.core.settings import settings
from org_automation.workflows.process_document import process_document

GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: new@example.com"


def test_clean_document_waits_for_human_with_before_and_after():
    r = process_document("doc-1", "client_update", GOOD, "evt-1")
    assert r.status == "needs_review" and r.approval_id
    item = list_pending()[0]
    assert item["kind"] == "change"
    assert item["proposal"]["crm_current"]["email"] == "old.client@example.com"   # BEFORE
    assert item["proposal"]["proposed_changes"]["email"] == "new@example.com"     # AFTER


def test_duplicate_event_is_ignored():
    process_document("doc-1", "client_update", GOOD, "evt-1")
    again = process_document("doc-1", "client_update", GOOD, "evt-1")
    assert again.status == "duplicate_ignored" and len(list_pending()) == 1


def test_problem_documents_go_to_exception_queue_not_the_void():
    r = process_document("doc-2", "client_update", "Client ID: C99999\nEffective date: 2026-10-12\nEmail: a@example.com", "evt-2")
    assert r.status == "needs_exception_review"
    assert {i.code for i in r.issues} == {"unknown_client"}
    assert list_pending()[0]["kind"] == "exception"


def test_crash_releases_the_claim_so_retry_works():
    class Boom:
        def extract(self, *a):
            raise RuntimeError("AI down")

    with pytest.raises(RuntimeError):
        process_document("doc-3", "client_update", GOOD, "evt-3", extractor=Boom())
    retry = process_document("doc-3", "client_update", GOOD, "evt-3")
    assert retry.status == "needs_review"


def test_audit_chain_detects_tampering():
    process_document("doc-1", "client_update", GOOD, "evt-1")
    process_document("doc-2", "client_update", GOOD, "evt-2")
    assert verify_chain() == {"ok": True, "events": 2}
    with sqlite3.connect(settings.database_path) as db:
        db.execute("UPDATE audit_events SET outcome='nothing to see here' WHERE seq=1")
    result = verify_chain()
    assert result["ok"] is False and result["first_bad_seq"] == 1


def test_audit_log_does_not_store_raw_sensitive_numbers():
    from org_automation.audit.writer import write_event
    write_event("note", "doc-x", "ok", {"text": "SSN 123-45-6789"})
    with sqlite3.connect(settings.database_path) as db:
        stored = db.execute("SELECT details_json FROM audit_events").fetchone()[0]
    assert "123-45-6789" not in stored
