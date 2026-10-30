"""PLAIN ENGLISH: Proves the WRITE_MODE switch. In shadow mode nothing can reach the CRM (the pretend one or, by the same
gate, Salesforce), yet the system still decides, checks for stale data and records what it WOULD have written. Real
systems default to shadow; a misspelled value stops the app from starting."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from org_automation.core.settings import Settings, settings
from org_automation.integrations import crm as crm_module
from org_automation.integrations.crm import CrmAdapter, CrmConflict, MockCrmAdapter, WriteGate
from org_automation.main import app
from org_automation.storage.database import connect

client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}
GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: new.client@example.com"


class TripwireCrm(CrmAdapter):
    """A CRM that fails the test if anyone writes to it."""
    def __init__(self, record=None):
        self.record, self.writes = record or {"email": "a@x.com", "effective_date": "2025-01-01"}, 0

    def get_record(self, record_id):
        return dict(self.record)

    def ping(self):
        return True

    def update_record(self, record_id, changes, idempotency_key, expected_current=None):
        self.writes += 1
        return {"record_id": record_id, "applied": True}


def test_default_is_safe_for_real_systems_and_explicit_choice_wins():
    assert Settings(_env_file=None).effective_write_mode() == "enabled"                       # pretend CRM: harmless
    assert Settings(_env_file=None, crm_provider="salesforce").effective_write_mode() == "shadow"
    assert Settings(_env_file=None, crm_provider="salesforce", write_mode="enabled").effective_write_mode() == "enabled"
    assert Settings(_env_file=None, write_mode="shadow").effective_write_mode() == "shadow"


def test_a_misspelled_mode_refuses_to_start_instead_of_guessing():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, write_mode="enable")


def test_gate_in_shadow_never_calls_the_connector_but_checks_honestly(monkeypatch):
    monkeypatch.setattr(settings, "write_mode", "shadow")
    inner = TripwireCrm()
    gate = WriteGate(inner)
    out = gate.update_record("C1", {"email": "b@x.com"}, "k1", expected_current={"email": "a@x.com"})
    assert out == {"record_id": "C1", "applied": False, "shadow": True, "would_apply": True} and inner.writes == 0
    assert gate.update_record("C1", {"email": "a@x.com"}, "k2")["would_apply"] is False      # already the current value
    with pytest.raises(CrmConflict):                                                          # same stale-data check as a real write
        gate.update_record("C1", {"email": "b@x.com"}, "k3", expected_current={"email": "someone-else@x.com"})
    assert inner.writes == 0


def test_gate_when_enabled_passes_writes_through(monkeypatch):
    monkeypatch.setattr(settings, "write_mode", "enabled")
    inner = TripwireCrm()
    assert WriteGate(inner).update_record("C1", {"email": "b@x.com"}, "k1")["applied"] is True and inner.writes == 1


def test_every_crm_from_the_factory_is_behind_the_gate_including_salesforce(monkeypatch):
    inner = TripwireCrm()
    monkeypatch.setattr(settings, "crm_provider", "salesforce")
    monkeypatch.setattr(crm_module, "_salesforce", inner)           # skip real login; the factory wraps whatever it holds
    crm = crm_module.get_crm()
    assert isinstance(crm, WriteGate) and settings.effective_write_mode() == "shadow"       # real provider, nothing set
    crm.update_record("C1", {"email": "b@x.com"}, "k1")
    assert inner.writes == 0
    crm_module.reset_crm_cache()


def test_shadow_approval_changes_nothing_and_records_what_would_have_happened(monkeypatch):
    monkeypatch.setattr(settings, "write_mode", "shadow")
    doc = client.post("/documents/doc-s/process", json={"document_type": "client_update", "event_id": "e-s", "text": GOOD}, headers=SVC).json()
    r = client.post(f"/approvals/{doc['approval_id']}/decision", json={"decision": "approve", "note": "verified"}, headers=ALICE)
    assert r.status_code == 200 and r.json()["crm_update"]["shadow"] is True and r.json()["crm_update"]["would_apply"] is True
    assert MockCrmAdapter().get_record("C10482")["email"] == "old.client@example.com"       # untouched
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mock_crm_updates").fetchone()[0] == 0
    events = client.get("/audit/events?document_id=doc-s", headers=ALICE).json()["items"]
    types = [e["event_type"] for e in events]
    assert "crm_update_shadowed" in types and "crm_update_applied" not in types
    shadow = next(e for e in events if e["event_type"] == "crm_update_shadowed")
    assert shadow["outcome"] == "would_apply" and shadow["details"]["fields"] == ["effective_date", "email"]
    assert "new.client@example.com" not in str(shadow["details"])                           # no client values in the log
    assert client.get("/documents/doc-s/workflow-status", headers=SVC).json()["state"] == "approved_shadow_not_written"
    assert client.get("/audit/verify", headers=ALICE).json()["ok"] is True


def test_screen_and_health_report_the_mode(monkeypatch):
    monkeypatch.setattr(settings, "write_mode", "shadow")
    assert client.get("/ui/config").json()["modes"]["writes"] == "shadow"
    assert client.get("/health/crm", headers=ALICE).json()["write_mode"] == "shadow"
    assert "Shadow mode." in client.get("/ui/app.js").text
