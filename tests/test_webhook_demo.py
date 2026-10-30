"""PLAIN ENGLISH: The signed "document changed" message from DocuSign now does real work. A completed envelope starts an
agent run (which re-checks signing with DocuSign itself); other events are only recorded; repeats and forgeries are
stopped. Also covers the development-only demo button where the server plays DocuSign."""
import json

from fastapi.testclient import TestClient

from org_automation.core.settings import settings
from org_automation.integrations.docusign import sign_hmac
from org_automation.main import app

client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}


def hook(envelope, event="envelope-completed", secret="test-secret"):
    body = json.dumps({"event": event, "data": {"envelopeId": envelope}}).encode()
    return client.post("/webhooks/docusign", content=body, headers={"X-DocuSign-Signature-1": sign_hmac(secret, body)})


def pending():
    return client.get("/approvals", headers=ALICE).json()["items"]


def event_types():
    return [e["event_type"] for e in client.get("/audit/events?limit=200", headers=ALICE).json()["items"]]


def test_completed_event_starts_an_agent_run_that_ends_at_a_human():
    r = hook("env-1001")
    assert r.status_code == 202 and r.json() == {"status": "accepted", "agent_run": "queued"}
    items = pending()
    assert len(items) == 1 and items[0]["submitted_by"] == "agent:docusign" and items[0]["kind"] == "change"
    assert "webhook_received" in event_types() and "agent_run_finished" in event_types()
    assert hook("env-1001").json()["status"] == "duplicate_ignored" and len(pending()) == 1


def test_a_completed_event_is_not_trusted_if_docusign_says_otherwise():
    assert hook("env-1003").status_code == 202           # the message claims "completed"; the envelope is still "sent"
    assert pending() == []                               # the agent re-checked and read nothing


def test_other_events_are_only_recorded():
    r = hook("env-1001", event="envelope-sent")
    assert r.json() == {"status": "accepted", "agent_run": "not_needed"}
    assert "agent_run_started" not in event_types() and pending() == []


def test_forged_event_is_rejected_and_does_nothing():
    assert hook("env-1001", secret="not-the-secret").status_code == 401
    assert pending() == [] and "webhook_received" not in event_types()


def test_demo_button_plays_docusign_through_the_real_receiver():
    r = client.post("/ui/demo/webhook", json={"envelope_id": "env-1001"}, headers=SVC).json()
    assert r["sent"]["forged"] is False and r["sent"]["body"]["data"]["envelopeId"] == "env-1001"
    assert "test-secret" not in json.dumps(r)
    assert r["received"]["http_status"] == 202
    run = r["received"]["body"]["agent_run"]
    assert run["driver"] == "scripted" and run["status"] == "awaiting_review" and len(pending()) == 1
    again = client.post("/ui/demo/webhook", json={"envelope_id": "env-1001"}, headers=SVC).json()
    assert again["received"]["body"]["status"] == "duplicate_ignored"


def test_demo_forgery_is_refused_and_demo_is_dev_service_only(monkeypatch):
    f = client.post("/ui/demo/webhook", json={"envelope_id": "env-1001", "forge": True}, headers=SVC).json()
    assert f["sent"]["forged"] is True and f["received"]["http_status"] == 401 and pending() == []
    assert client.post("/ui/demo/webhook", json={"envelope_id": "env-1001"}, headers=ALICE).status_code == 403
    assert client.post("/ui/demo/webhook", json={"envelope_id": "../x"}, headers=SVC).status_code == 422
    monkeypatch.setattr(settings, "environment", "production")
    assert client.post("/ui/demo/webhook", json={"envelope_id": "env-1001"}, headers=SVC).status_code == 403
