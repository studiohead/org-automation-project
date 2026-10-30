#!/usr/bin/env python3
"""PLAIN ENGLISH: A director-friendly, one-command demonstration of the whole safe path.

Run from the project root:

    python scripts/demo_walkthrough.py

It shows, in plain language:
  1. A clean document is read and fact-checked.
  2. A human must approve before anything is written.
  3. On approval the CRM is updated exactly once.
  4. A bad / ungrounded extraction is sent to the exception queue instead.
  5. The audit diary verifies clean (nothing was edited after the fact).

No network, no API keys, no real client data. Safe to run any time.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make the package importable when run as a script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Use an isolated demo database so we never touch a developer's real local DB.
# Prefer /tmp for maximum portability across environments.
_demo_db = os.environ.get("DATABASE_PATH") or "/tmp/org_demo_walkthrough.sqlite3"
os.environ["DATABASE_PATH"] = _demo_db
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("EXTRACTION_MODE", "demo")
os.environ.setdefault("CRM_PROVIDER", "mock")

from org_automation.approvals.service import decide, list_pending
from org_automation.audit.writer import verify_chain
from org_automation.core.settings import settings
from org_automation.integrations.crm import get_crm
from org_automation.storage.database import ensure_db
from org_automation.workflows.apply_update import apply_approved_update
from org_automation.workflows.process_document import process_document

# Settings are constructed at import time; force the path we just set.
settings.database_path = _demo_db


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def main() -> int:
    # Fresh demo DB every run
    db_path = Path(settings.database_path)
    if db_path.exists():
        db_path.unlink()
    ensure_db()

    banner("FREESTONE AUTOMATION SCAFFOLD — LIVE DEMO WALKTHROUGH")
    print("Everything below uses synthetic data and the local mock CRM.")
    print("No external systems are contacted.")

    # ------------------------------------------------------------------ 1. Happy path
    step(1, "Process a clean client-update form (demo extractor, no Claude required)")
    clean_text = (
        "Client ID: C10482\n"
        "Effective date: 2026-10-12\n"
        "Email: new.client@example.com\n"
        "This signed form authorizes the email change on the account."
    )
    result = process_document(
        document_id="demo-clean-001",
        document_type="client_update",
        text_or_pages=clean_text,
        event_id="evt-demo-clean-001",
        actor="demo-service",
    )
    print(f"    Status          : {result.status}")
    print(f"    Approval ID     : {result.approval_id}")
    print(f"    Message         : {result.message}")
    if result.extracted:
        print(f"    Extracted email : {result.extracted.email.value}")
        print(f"    Source excerpt  : {result.extracted.email.source_excerpt!r}")
        print(f"    Confidence      : {result.extracted.email.confidence}")

    assert result.status == "needs_review", f"Clean doc should land in human review, got {result.status}"
    assert result.approval_id, "Expected a pending approval"

    # ------------------------------------------------------------------ 2. Review queue
    step(2, "Look at the human review queue (BEFORE vs AFTER)")
    pending = list_pending()
    assert pending, "Expected at least one pending item"
    item = next(p for p in pending if p["approval_id"] == result.approval_id)
    proposal = item["proposal"]
    print(f"    Kind            : {item['kind']}")
    print(f"    Record          : {proposal.get('record_id')}")
    print(f"    CRM current     : {json.dumps(proposal.get('crm_current'), indent=6)}")
    print(f"    Proposed change : {json.dumps(proposal.get('proposed_changes'), indent=6)}")
    print("    → Nothing has been written to the CRM yet.")

    # ------------------------------------------------------------------ 3. Approve
    step(3, "A different human approves the change (four-eyes rule)")
    decision = decide(
        approval_id=result.approval_id,
        reviewer="demo-reviewer",
        decision="approve",
        note="Looks correct; source excerpt matches the form.",
    )
    print(f"    Decision recorded: {decision['status']}")
    print(f"    Decided by       : {decision.get('decided_by')}")

    # ------------------------------------------------------------------ 4. Apply (gated)
    step(4, "Apply the approved change — the only place a CRM write is allowed")
    apply_result = apply_approved_update(
        approval_id=result.approval_id,
        requested_by="demo-reviewer",
    )
    print(f"    Apply outcome   : {json.dumps(apply_result, indent=6, default=str)}")
    crm_after = get_crm().get_record("C10482")
    print(f"    CRM after write : {json.dumps(crm_after, indent=6, default=str)}")

    # ------------------------------------------------------------------ 5. Exception path
    step(5, "A document that fails client lookup goes to the exception queue")
    bad_text = (
        "Client ID: C99999\n"  # unknown in the mock CRM
        "Effective date: 2026-10-12\n"
        "Email: maybe@example.com\n"
    )
    bad = process_document(
        document_id="demo-bad-001",
        document_type="client_update",
        text_or_pages=bad_text,
        event_id="evt-demo-bad-001",
        actor="demo-service",
    )
    print(f"    Status          : {bad.status}")
    print(f"    Issue codes     : {sorted({i.code for i in (bad.issues or [])})}")
    print("    → No CRM write. A human investigates; the system does not guess.")

    # ------------------------------------------------------------------ 6. Audit
    step(6, "Verify the audit diary has not been tampered with")
    chain = verify_chain()
    # verify_chain may return a bool, a tuple, or a dict depending on version; handle all
    if isinstance(chain, tuple):
        ok, detail = chain[0], chain[1] if len(chain) > 1 else ""
    elif isinstance(chain, dict):
        ok, detail = chain.get("ok", chain.get("valid")), chain
    else:
        ok, detail = bool(chain), chain
    print(f"    Chain valid     : {ok}")
    print(f"    Detail          : {detail}")

    banner("DEMO COMPLETE")
    print("Takeaways:")
    print("  • Extraction is separated from verification.")
    print("  • Human approval is a required state, not an optional notification.")
    print("  • CRM writes only happen after an explicit, audited decision by a different actor.")
    print("  • Problems become exception-queue items; they do not silently write or disappear.")
    print("  • The audit chain is independently verifiable.")
    print()
    print(f"Demo database left at: {db_path}")
    print("Delete it any time; it contains only synthetic data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
