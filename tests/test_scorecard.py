"""PLAIN ENGLISH: Checks the scorecard's counting and, above all, the agreement check: staff and the system reaching the same
answer, staff doing something different, nothing done yet, and changes the system wrote itself being set aside."""
import pytest
from fastapi.testclient import TestClient

from org_automation.core.settings import settings
from org_automation.integrations.crm import CrmUnavailable, MockCrmAdapter
from org_automation.main import app
from org_automation.reporting import scorecard as sc
from org_automation.reporting.scorecard import classify
from org_automation.storage.database import connect

client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}
DATE = "2026-10-12"


def submit(doc, client_id, email):
    text = f"Client ID: {client_id}\nEffective date: {DATE}\nEmail: {email}"
    return client.post(f"/documents/{doc}/process", json={"document_type": "client_update", "event_id": f"e-{doc}", "text": text}, headers=SVC).json()


def approve(approval_id):
    return client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve", "note": "verified"}, headers=ALICE)


def staff_edit(record_id, email):
    return client.post("/ui/demo/staff-edit", json={"record_id": record_id, "email": email, "effective_date": DATE}, headers=ALICE)


def card(days=30):
    return client.get(f"/scorecard?days={days}", headers=ALICE).json()


def test_classify_covers_every_outcome():
    base, prop = {"email": "a", "effective_date": "1"}, {"email": "b", "effective_date": "2"}
    assert classify(base, prop, {"email": "b", "effective_date": "2"}) == ("agreed", [])
    assert classify(base, prop, {"email": "a", "effective_date": "1"}) == ("untouched", [])
    assert classify(base, prop, {"email": "b", "effective_date": "1"}) == ("partial", [])
    verdict, diffs = classify(base, prop, {"email": "z", "effective_date": "1"})
    assert verdict == "different" and diffs == [{"field": "email", "proposed": "b", "crm_before": "a", "crm_now": "z"}]
    assert classify(base, dict(base), dict(base)) == ("no_change", [])


def test_shadow_trial_compares_proposals_with_what_staff_really_did(monkeypatch):
    monkeypatch.setattr(settings, "write_mode", "shadow")
    a = submit("doc-a", "C10482", "new-a@example.com")["approval_id"]
    submit("doc-b", "C10001", "new-b@example.com")
    submit("doc-c", "C20005", "new-c@example.com")
    submit("doc-x", "C99999", "x@example.com")
    assert approve(a).json()["crm_update"]["shadow"] is True
    assert staff_edit("C10482", "new-a@example.com").status_code == 200       # staff made the same change
    assert staff_edit("C10001", "something-else@example.com").status_code == 200   # staff did something different
    r = card()
    ag = r["agreement"]
    assert (ag["agreed"], ag["different"], ag["untouched"], ag["partial"], ag["applied_by_system"]) == (1, 1, 1, 0, 0)
    assert ag["judged"] == 2 and ag["rate"] == 0.5
    [d] = r["disagreements"]
    assert d["document_id"] == "doc-b" and d["differences"][0]["proposed"] == "new-b@example.com"
    assert d["differences"][0]["crm_now"] == "something-else@example.com"
    assert r["volume"]["changes_proposed"] == 3 and r["volume"]["exceptions"] == 1 and r["volume"]["documents"] == 4
    assert r["exceptions_by_reason"] == {"unknown_client": 1}
    assert r["reviews"]["approved"] == 1 and r["reviews"]["pending"] == 3 and r["reviews"]["median_minutes_to_decision"] is not None
    assert r["shadow"] == {"would_apply": 1, "already_current": 0} and r["write_mode"] == "shadow"
    assert MockCrmAdapter().get_record("C20005")["email"] == "three@example.com"          # the scorecard wrote nothing


def test_changes_the_system_wrote_itself_prove_nothing(monkeypatch):
    monkeypatch.setattr(settings, "write_mode", "enabled")
    approve(submit("doc-w", "C10482", "new-w@example.com")["approval_id"])
    ag = card()["agreement"]
    assert ag["applied_by_system"] == 1 and ag["agreed"] == 0 and ag["rate"] is None


def test_unreadable_crm_is_counted_not_fatal(monkeypatch):
    submit("doc-u", "C10482", "new-u@example.com")

    class Down:
        def get_record(self, record_id):
            raise CrmUnavailable("down")
    monkeypatch.setattr(sc, "get_crm", lambda: Down())
    r = client.get("/scorecard", headers=ALICE)
    assert r.status_code == 200 and r.json()["agreement"]["unavailable"] == 1


def test_window_excludes_old_work_and_endpoint_is_reviewer_only():
    submit("doc-old", "C10482", "new-o@example.com")
    with connect() as db:
        db.execute("UPDATE approvals SET created_at='2020-01-01T00:00:00+00:00'")
    assert card(30)["volume"]["changes_proposed"] == 0
    assert client.get("/scorecard", headers=SVC).status_code == 403
    assert client.get("/scorecard?days=0", headers=ALICE).status_code == 422


def test_time_estimate_only_appears_when_the_firm_supplies_its_own_number(monkeypatch):
    submit("doc-t", "C10482", "new-t@example.com")
    assert card()["estimate"] is None
    monkeypatch.setattr(settings, "manual_minutes_per_update", 12.0)
    assert card()["estimate"] == {"manual_minutes_per_update": 12.0, "hours_of_manual_work_covered": 0.2}


def test_staff_edit_helper_is_dev_pretend_crm_reviewer_only(monkeypatch):
    body = {"record_id": "C10482", "email": "a@b.co", "effective_date": DATE}
    assert client.post("/ui/demo/staff-edit", json=body, headers=SVC).status_code == 403
    monkeypatch.setattr(settings, "crm_provider", "salesforce")
    assert client.post("/ui/demo/staff-edit", json=body, headers=ALICE).status_code == 403
    monkeypatch.setattr(settings, "crm_provider", "mock")
    monkeypatch.setattr(settings, "environment", "production")
    assert client.post("/ui/demo/staff-edit", json=body, headers=ALICE).status_code == 403
    monkeypatch.setattr(settings, "environment", "development")
    assert client.post("/ui/demo/staff-edit", json={**body, "record_id": "C99999"}, headers=ALICE).status_code == 422
    assert client.post("/ui/demo/staff-edit", json=body, headers=ALICE).status_code == 200
    assert "demo_staff_edit" in [e["event_type"] for e in client.get("/audit/events", headers=ALICE).json()["items"]]


@pytest.mark.parametrize("path", ["/scorecard"])
def test_scorecard_needs_a_key(path):
    assert client.get(path).status_code == 401
