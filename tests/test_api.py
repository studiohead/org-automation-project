"""PLAIN ENGLISH: End-to-end tests through the real web endpoints: who may do what, the approve/reject
flow, DocuSign webhook signature checks, PDF upload, and the SQL report queries."""
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from org_automation.core.settings import settings
from org_automation.main import app

client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}
GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: new@example.com"


def submit(event_id="evt-1", text=GOOD, doc="doc-1"):
    body = {"document_type": "client_update", "event_id": event_id, "text": text}
    return client.post(f"/documents/{doc}/process", json=body, headers=SVC)


def test_health_is_public():
    assert client.get("/health").json() == {"status": "ok"}


def test_auth_rules():
    assert client.post("/documents/d/process", json={}).status_code == 401
    assert client.get("/approvals", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/approvals", headers=SVC).status_code == 403          # service can't review
    assert client.post("/documents/d/process", json={}, headers=ALICE).status_code == 403  # reviewer can't submit


def test_approve_flow_updates_crm_once_and_is_audited():
    approval_id = submit().json()["approval_id"]
    assert len(client.get("/approvals", headers=ALICE).json()["items"]) == 1
    r = client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve", "note": "matches form"}, headers=ALICE)
    assert r.status_code == 200 and r.json()["crm_update"]["applied"] is True
    # deciding twice is refused
    assert client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=ALICE).status_code == 409
    # the "CRM" now holds the new email, and the diary names who approved
    from org_automation.integrations.crm import MockCrmAdapter
    assert MockCrmAdapter().get_record("C10482")["email"] == "new@example.com"
    with sqlite3.connect(settings.database_path) as db:
        actors = [r[0] for r in db.execute("SELECT actor FROM audit_events WHERE event_type='crm_update_applied'")]
    assert actors == ["alice"]
    assert client.get("/audit/verify", headers=ALICE).json()["ok"] is True


def test_reject_does_not_touch_crm():
    approval_id = submit().json()["approval_id"]
    r = client.post(f"/approvals/{approval_id}/decision", json={"decision": "reject", "note": "wrong form"}, headers=ALICE)
    assert r.json()["status"] == "rejected" and r.json()["crm_update"] is None
    from org_automation.integrations.crm import MockCrmAdapter
    assert MockCrmAdapter().get_record("C10482")["email"] == "old.client@example.com"


def test_exception_items_cannot_be_approved():
    r = submit(text="Client ID: C99999\nEffective date: 2026-10-12\nEmail: a@example.com").json()
    assert r["status"] == "needs_exception_review"
    resp = client.post(f"/approvals/{r['approval_id']}/decision", json={"decision": "approve"}, headers=ALICE)
    assert resp.status_code == 409


def test_bad_decision_value_rejected():
    approval_id = submit().json()["approval_id"]
    assert client.post(f"/approvals/{approval_id}/decision", json={"decision": "maybe"}, headers=ALICE).status_code == 422


def _sign(body: bytes, secret="test-secret") -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_docusign_webhook_signature_and_replay():
    body = json.dumps({"event": "envelope-completed", "data": {"envelopeId": "env-1"}}).encode()
    assert client.post("/webhooks/docusign", content=body).status_code == 401
    assert client.post("/webhooks/docusign", content=body, headers={"X-DocuSign-Signature-1": _sign(body, "wrong")}).status_code == 401
    ok = client.post("/webhooks/docusign", content=body, headers={"X-DocuSign-Signature-1": _sign(body)})
    assert ok.status_code == 202 and ok.json()["status"] == "accepted"
    replay = client.post("/webhooks/docusign", content=body, headers={"X-DocuSign-Signature-1": _sign(body)})
    assert replay.json()["status"] == "duplicate_ignored"


def test_docusign_webhook_refuses_everything_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "docusign_hmac_secret", None)
    assert client.post("/webhooks/docusign", content=b"{}").status_code == 503


def _tiny_pdf(lines: list[str]) -> bytes:
    stream = "BT /F1 12 Tf 14 TL 72 720 Td " + " T* ".join(f"({line}) Tj" for line in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{off:010d} 00000 n \n" for off in offsets).encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


def test_pdf_upload_is_read_and_processed():
    pdf = _tiny_pdf(["Client ID: C10482", "Effective date: 2026-10-12", "Email: pdf@example.com"])
    r = client.post("/documents/pdf-1/process-pdf", headers=SVC, files={"file": ("f.pdf", pdf, "application/pdf")},
                    data={"document_type": "client_update", "event_id": "evt-pdf"})
    assert r.status_code == 200 and r.json()["status"] == "needs_review"
    assert r.json()["extracted"]["email"]["value"] == "pdf@example.com"


def test_non_pdf_upload_rejected():
    r = client.post("/documents/x/process-pdf", headers=SVC, files={"file": ("f.pdf", b"not a pdf", "application/pdf")},
                    data={"document_type": "client_update", "event_id": "e"})
    assert r.status_code == 422


def test_sql_reporting_queries_run():
    submit()
    sql = (Path(__file__).resolve().parent.parent / "sql" / "reporting_queries.sql").read_text()
    with sqlite3.connect(settings.database_path) as db:
        for stmt in [s for s in sql.split(";") if "SELECT" in s]:
            db.execute(stmt).fetchall()   # raises if any query is broken
