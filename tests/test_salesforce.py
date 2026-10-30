"""PLAIN ENGLISH: Tests for the Salesforce connection, run against a simulated org: logging in, finding clients,
refusing to guess, updating safely (no double-writes, no overwriting a colleague's edit), handling outages, and
making sure secrets and client data never leak into logs or error messages."""
import sqlite3

import httpx
import pytest
from fake_salesforce import DOMAIN, FakeSalesforceOrg
from fastapi.testclient import TestClient

from org_automation.approvals.service import list_pending
from org_automation.core.settings import settings
from org_automation.integrations import crm as crm_module
from org_automation.integrations.crm import CrmAmbiguous, CrmConflict, CrmError, CrmUnavailable
from org_automation.integrations.salesforce import SalesforceCrmAdapter
from org_automation.main import app
from org_automation.workflows.apply_update import ApplyError
from org_automation.workflows.process_document import process_document
from org_automation.workflows.review_decision import review

SECRET = "s3cret"


def make(org: FakeSalesforceOrg, **kw) -> SalesforceCrmAdapter:
    return SalesforceCrmAdapter(domain=DOMAIN, client_id="cid", client_secret=SECRET, transport=org.transport(), **kw)


GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: new@example.com"


# ------------------------------------------------------------------ reading
def test_lookup_returns_only_the_two_approved_fields_and_asks_for_only_those():
    org = FakeSalesforceOrg()
    sf = make(org)
    assert sf.get_record("C10482") == {"email": "old.client@example.com", "effective_date": "2025-01-01"}
    assert org.queries == ["SELECT Id, Email, Email_Effective_Date__c FROM Contact WHERE Client_ID__c = 'C10482' LIMIT 2"]
    assert sf.get_record("C99999") is None


def test_hostile_client_numbers_never_reach_salesforce():
    org = FakeSalesforceOrg()
    sf = make(org)
    for bad in ["C1' OR Name != '", "C1; DROP", "", "x" * 51, "a b"]:
        assert sf.get_record(bad) is None
    assert org.queries == []


def test_duplicate_client_numbers_are_never_guessed():
    org = FakeSalesforceOrg(records=[{"Id": "003A", "Client_ID__c": "C1", "Email": "a@x.io", "Email_Effective_Date__c": None},
                                     {"Id": "003B", "Client_ID__c": "C1", "Email": "b@x.io", "Email_Effective_Date__c": None}])
    with pytest.raises(CrmAmbiguous):
        make(org).get_record("C1")
    org.records.append({"Id": "9", "Client_ID__c": "C10482", "Email": "o@x.io", "Email_Effective_Date__c": None})
    r = process_document("d", "client_update", "Client ID: C1\nEffective date: 2026-10-12\nEmail: n@x.io", "e1", crm=make(org))
    assert r.status == "needs_exception_review" and "ambiguous_client" in {i.code for i in r.issues}


# ------------------------------------------------------------------ login & tokens
def test_login_happens_once_and_token_is_reused():
    org = FakeSalesforceOrg()
    sf = make(org)
    sf.get_record("C10482"), sf.get_record("C10001")
    assert org.token_n == 1


def test_expired_token_is_refreshed_once_automatically():
    org = FakeSalesforceOrg()
    sf = make(org)
    sf.get_record("C10482")
    org.expire_token()
    assert sf.get_record("C10482")["email"] == "old.client@example.com" and org.token_n == 2


def test_bad_credentials_and_outages_become_safe_retryable_errors():
    org = FakeSalesforceOrg(secret="different")
    with pytest.raises(CrmUnavailable) as exc:
        make(org).get_record("C10482")
    assert SECRET not in str(exc.value) and "acme" not in str(exc.value)
    org = FakeSalesforceOrg()
    org.fail_with = 503
    with pytest.raises(CrmUnavailable) as exc:
        make(org).get_record("C10482")
    assert "client data here" not in str(exc.value)                 # Salesforce's message text is never echoed
    org.fail_with = 400
    with pytest.raises(CrmError) as exc:
        make(org).get_record("C10482")
    assert not isinstance(exc.value, CrmUnavailable) and "SERVER_ERROR" in str(exc.value)   # error CODE only


def test_only_salesforce_https_hosts_are_accepted():
    kw = {"client_id": "c", "client_secret": "s"}
    for bad in ["http://acme.my.salesforce.com", "https://evil.example.com", "https://acme.my.salesforce.com.evil.io",
                "https://user@acme.my.salesforce.com", "https://acme.my.salesforce.com:8443"]:
        with pytest.raises(CrmError):
            SalesforceCrmAdapter(domain=bad, **kw)
    SalesforceCrmAdapter(domain="https://acme--sandbox.sandbox.my.salesforce.com", **kw)
    for bad_name in [{"sobject": "Contact; DROP"}, {"client_id_field": "A' OR '1"}, {"api_version": "v62"}]:
        with pytest.raises(CrmError):
            SalesforceCrmAdapter(domain=DOMAIN, **kw, **bad_name)
    with pytest.raises(CrmError):
        SalesforceCrmAdapter(domain=DOMAIN, client_id="", client_secret="")


def test_token_is_never_sent_to_a_non_salesforce_instance_url():
    org = FakeSalesforceOrg()
    original = org.handle

    def evil(request):
        r = original(request)
        if request.url.path == "/services/oauth2/token" and r.status_code == 200:
            return httpx.Response(200, json={"access_token": "t", "instance_url": "https://evil.example.com"})
        return r

    sf = SalesforceCrmAdapter(domain=DOMAIN, client_id="c", client_secret=SECRET,
                              transport=httpx.MockTransport(evil))
    with pytest.raises(CrmError):
        sf.get_record("C10482")


def test_ping():
    assert make(FakeSalesforceOrg()).ping() is True


# ------------------------------------------------------------------ writing
def test_update_writes_only_mapped_fields_and_is_idempotent():
    org = FakeSalesforceOrg()
    sf = make(org)
    changes = {"email": "new@example.com", "effective_date": "2026-10-12"}
    seen = {"email": "old.client@example.com", "effective_date": "2025-01-01"}
    assert sf.update_record("C10482", changes, "k1", expected_current=seen) == {"record_id": "C10482", "applied": True}
    assert org.patches == [("003A", {"Email": "new@example.com", "Email_Effective_Date__c": "2026-10-12"})]
    assert sf.update_record("C10482", changes, "k1", expected_current=seen)["applied"] is False   # retry = no-op
    assert len(org.patches) == 1
    with pytest.raises(CrmError):
        sf.update_record("C10482", {"name": "Hacked"}, "k2")                                       # unmapped field refused
    with pytest.raises(KeyError):
        sf.update_record("C99999", changes, "k3")


def test_update_refuses_to_overwrite_a_colleagues_edit():
    org = FakeSalesforceOrg()
    sf = make(org)
    org.records[0]["Email"] = "colleague.edit@example.com"        # changed in Salesforce after the reviewer looked
    with pytest.raises(CrmConflict):
        sf.update_record("C10482", {"email": "new@example.com"}, "k1",
                         expected_current={"email": "old.client@example.com"})
    assert org.patches == []


# ------------------------------------------------------------------ end to end with the real workflow
def test_full_flow_against_salesforce():
    org = FakeSalesforceOrg()
    sf = make(org)
    r = process_document("doc-sf", "client_update", GOOD, "evt-sf", crm=sf)
    assert r.status == "needs_review"
    item = list_pending()[0]
    assert item["proposal"]["crm_current"] == {"email": "old.client@example.com", "effective_date": "2025-01-01"}
    assert org.patches == []                                       # nothing written before a human decides
    out = review(item["approval_id"], "alice", "approve", "ok", crm=sf)
    assert out["crm_update"]["applied"] is True and org.records[0]["Email"] == "new@example.com"


def test_conflict_after_approval_blocks_the_write_and_is_audited():
    org = FakeSalesforceOrg()
    sf = make(org)
    process_document("doc-sf", "client_update", GOOD, "evt-sf", crm=sf)
    aid = list_pending()[0]["approval_id"]
    org.records[0]["Email"] = "someone.else@example.com"           # edited during the review window
    with pytest.raises(ApplyError):
        review(aid, "alice", "approve", "ok", crm=sf)
    assert org.patches == [] and org.records[0]["Email"] == "someone.else@example.com"
    with sqlite3.connect(settings.database_path) as db:
        events = [r[0] for r in db.execute("SELECT event_type FROM audit_events")]
    assert "crm_update_conflict" in events


def test_secret_never_appears_in_audit_log_or_proposals():
    org = FakeSalesforceOrg()
    sf = make(org)
    process_document("doc-sf", "client_update", GOOD, "evt-sf", crm=sf)
    review(list_pending()[0]["approval_id"], "alice", "approve", "ok", crm=sf)
    with sqlite3.connect(settings.database_path) as db:
        dump = " ".join(str(row) for t in ("audit_events", "approvals") for row in db.execute(f"SELECT * FROM {t}"))
    assert SECRET not in dump and "tok-1" not in dump


# ------------------------------------------------------------------ provider switch & API
def test_factory_selects_provider_and_requires_configuration(monkeypatch):
    crm_module.reset_crm_cache()
    assert type(crm_module.get_crm().inner).__name__ == "MockCrmAdapter"      # every CRM sits behind the write gate
    monkeypatch.setattr(settings, "crm_provider", "salesforce")
    with pytest.raises(CrmError):                                   # selected but not configured -> clear failure
        crm_module.get_crm()
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "salesforce_domain", DOMAIN)
    monkeypatch.setattr(settings, "salesforce_client_id", "cid")
    monkeypatch.setattr(settings, "salesforce_client_secret", SecretStr(SECRET))
    assert isinstance(crm_module.get_crm().inner, SalesforceCrmAdapter)
    crm_module.reset_crm_cache()


def test_api_crm_health_and_apply_conflict_status(monkeypatch):
    client = TestClient(app)
    alice, svc = {"X-API-Key": "k-alice"}, {"X-API-Key": "k-service"}
    assert client.get("/health/crm", headers=alice).json() == {"provider": "mock", "write_mode": "enabled", "ok": True}
    assert client.get("/health/crm", headers=svc).status_code == 403
    org = FakeSalesforceOrg()
    sf = make(org)
    monkeypatch.setattr(crm_module, "_salesforce", sf)
    monkeypatch.setattr(settings, "crm_provider", "salesforce")
    assert client.get("/health/crm", headers=alice).json() == {"provider": "salesforce", "write_mode": "shadow", "ok": True}   # real CRM: safe default
    monkeypatch.setattr(settings, "write_mode", "enabled")      # this test is about the real write path's stale-data protection
    body = {"document_type": "client_update", "event_id": "e-api", "text": GOOD}
    aid = client.post("/documents/d-api/process", json=body, headers=svc).json()["approval_id"]
    org.records[0]["Email"] = "edited.meanwhile@example.com"
    r = client.post(f"/approvals/{aid}/decision", json={"decision": "approve", "note": "checked"}, headers=alice)
    assert r.status_code == 409 and "not applied" in r.json()["detail"] and org.patches == []
    # the conflict is not a dead end: the item is marked stale and the document reads as needing resubmission
    assert client.get("/documents/d-api/workflow-status", headers=alice).json()["state"] == "stale_needs_resubmission"
    assert client.post(f"/approvals/{aid}/decision", json={"decision": "approve", "note": "again"}, headers=alice).status_code == 409
    org.fail_with = 503
    assert client.get("/health/crm", headers=alice).json()["ok"] is False
    crm_module.reset_crm_cache()
