"""PLAIN ENGLISH: Checks for the Microsoft Graph connector, using pretend Microsoft servers (no network, no real tenant).

Proves: tokens are cached and refreshed, a rejected token is retried once, filing is safe to repeat, failures never leak
secrets, a real archive respects the write gate, and a filing problem can never undo a human's approval."""
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from org_automation.audit.writer import verify_chain
from org_automation.core.settings import settings
from org_automation.integrations import microsoft_graph as mg
from org_automation.integrations.oauth import ClientCredentialsTokenProvider, TokenError
from org_automation.main import app
from org_automation.workflows.apply_update import apply_approved_update
from org_automation.workflows.archive_record import build_record

GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: client@example.com"
ALICE, SVC = {"X-API-Key": "k-alice"}, {"X-API-Key": "k-service"}
SECRET = "s3cr3t-value-never-log-me"
SITE = "contoso.sharepoint.com,11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def _tokens(handler, clock=None):
    return ClientCredentialsTokenProvider(tenant_id="contoso.onmicrosoft.com", client_id="app-id", client_secret=SECRET,
                                          transport=httpx.MockTransport(handler), clock=clock or Clock())


class FakeTokens:
    def __init__(self):
        self.issued, self.invalidated = 0, 0

    def get_access_token(self, audience, scopes=None):
        self.issued += 1
        return f"tok-{self.issued}"

    def invalidate(self):
        self.invalidated += 1


def _adapter(handler, tokens=None):
    return mg.GraphSharePointAdapter(site_id=SITE, folder="Client Updates/2026", tokens=tokens or FakeTokens(),
                                     transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ tokens
def test_token_is_cached_until_near_expiry_then_refreshed():
    calls, clock = [], Clock()

    def handler(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json={"access_token": f"t{len(calls)}", "expires_in": 3600})
    p = _tokens(handler, clock)
    assert p.get_access_token(mg.GRAPH_AUDIENCE) == "t1" and p.get_access_token(mg.GRAPH_AUDIENCE) == "t1"
    assert len(calls) == 1
    req = calls[0]
    assert req.url.host == "login.microsoftonline.com" and "/contoso.onmicrosoft.com/oauth2/v2.0/token" in req.url.path
    assert b"graph.microsoft.com%2F.default" in req.content or b"graph.microsoft.com/.default" in req.content
    clock.now = 3600 - 30                     # inside the 60-second safety margin: must refresh
    assert p.get_access_token(mg.GRAPH_AUDIENCE) == "t2"


def test_token_errors_never_contain_the_secret_or_response_body():
    p = _tokens(lambda r: httpx.Response(401, text=f"bad secret {SECRET}"))
    with pytest.raises(TokenError) as err:
        p.get_access_token(mg.GRAPH_AUDIENCE)
    assert SECRET not in str(err.value) and "401" in str(err.value)


def test_malformed_tenant_cannot_redirect_the_secret_elsewhere():
    for bad in ("evil.example/x?", "a/../b", "", "x@y"):
        with pytest.raises(TokenError):
            ClientCredentialsTokenProvider(tenant_id=bad, client_id="a", client_secret="b")


# ------------------------------------------------------------------ adapter
def test_upload_uses_site_folder_and_fail_on_conflict():
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(201, json={"id": "ITEM-1"})
    receipt = _adapter(handler).archive_record("d1", "apr-123", b"{}")
    req = seen["req"]
    assert receipt == mg.ArchiveReceipt("ITEM-1", False)
    assert req.method == "PUT" and req.url.host == "graph.microsoft.com"
    assert "/drive/root:/Client%20Updates/2026/d1-apr-123.json:/content" in req.url.raw_path.decode()
    assert req.url.params["@microsoft.graph.conflictBehavior"] == "fail"
    assert req.headers["authorization"] == "Bearer tok-1"


def test_retry_after_conflict_confirms_existing_copy_instead_of_duplicating():
    def handler(req: httpx.Request):
        if req.method == "PUT":
            return httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
        return httpx.Response(200, json={"id": "ITEM-1"})
    assert _adapter(handler).archive_record("d1", "apr-123", b"{}") == mg.ArchiveReceipt("ITEM-1", True)


def test_rejected_token_is_replaced_and_retried_exactly_once():
    attempts, tokens = [], FakeTokens()

    def handler(req: httpx.Request):
        attempts.append(req.headers["authorization"])
        return httpx.Response(401) if len(attempts) == 1 else httpx.Response(201, json={"id": "X"})
    assert _adapter(handler, tokens).archive_record("d", "a", b"{}").drive_item_id == "X"
    assert attempts == ["Bearer tok-1", "Bearer tok-2"] and tokens.invalidated == 1


def test_persistent_401_gives_up_after_one_retry():
    attempts = []

    def handler(req):
        attempts.append(1)
        return httpx.Response(401)
    with pytest.raises(mg.GraphUnavailable):
        _adapter(handler).archive_record("d", "a", b"{}")
    assert len(attempts) == 2


@pytest.mark.parametrize("status,exc", [(429, mg.GraphUnavailable), (503, mg.GraphUnavailable), (403, mg.GraphError)])
def test_errors_report_status_only_and_throttling_is_retryable(status, exc):
    with pytest.raises(exc) as err:
        _adapter(lambda r: httpx.Response(status, text="client Jane Roe at secret-name.json")).archive_record("d", "a", b"{}")
    assert str(status) in str(err.value) and "Jane" not in str(err.value) and "secret-name" not in str(err.value)
    assert (status != 403) == isinstance(err.value, mg.GraphUnavailable)


def test_network_failure_is_a_retryable_error_without_url_details():
    def handler(req):
        raise httpx.ConnectError("boom to https://graph.microsoft.com/secret")
    with pytest.raises(mg.GraphUnavailable) as err:
        _adapter(handler).ping()
    assert "ConnectError" in str(err.value) and "secret" not in str(err.value)


def test_redirects_are_not_followed_so_the_token_cannot_leak():
    def handler(req):
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})
    with pytest.raises(mg.GraphError):
        _adapter(handler).archive_record("d", "a", b"{}")


def test_unsafe_site_folder_and_file_names_are_rejected_or_neutralised():
    tokens = FakeTokens()
    for site in ("", "a b", "../x", "site/with/slash"):
        with pytest.raises(mg.GraphError):
            mg.GraphSharePointAdapter(site_id=site, folder="Ok", tokens=tokens)
    for folder in ("", "..", "a/../b", "bad:name", "a//" + "x" * 80):
        with pytest.raises(mg.GraphError):
            mg.GraphSharePointAdapter(site_id=SITE, folder=folder, tokens=tokens)
    name = mg.safe_file_name("../../etc/passwd", "apr/../1")
    assert "/" not in name and ".." not in name and name.endswith(".json")
    assert mg.safe_file_name("???", "x").startswith("document-")


def test_base_url_must_be_microsoft_graph():
    with pytest.raises(mg.GraphError):
        mg.GraphSharePointAdapter(site_id=SITE, folder="Ok", tokens=FakeTokens(), base_url="https://evil.example/v1.0")


# ------------------------------------------------------------------ workflow: archive after approval
def _submit_and_approve(client, doc="d1", event="e1", expect=200):
    aid = client.post(f"/documents/{doc}/process", json={"document_type": "client_update", "event_id": event, "text": GOOD},
                      headers=SVC).json()["approval_id"]
    r = client.post(f"/approvals/{aid}/decision", json={"decision": "approve", "note": "checked against the form"}, headers=ALICE)
    assert r.status_code == expect, r.text
    return aid, r


def _events(kind):
    with sqlite3.connect(settings.database_path) as db:
        return db.execute("SELECT outcome, details_json FROM audit_events WHERE event_type=?", (kind,)).fetchall()


def test_default_provider_none_files_nothing():
    _, r = _submit_and_approve(TestClient(app))
    assert r.json()["crm_update"]["archive"] == {"status": "disabled"} and _events("document_archived") == []


def test_mock_archive_files_one_redacted_record_and_retry_is_safe(monkeypatch):
    monkeypatch.setattr(settings, "archive_provider", "mock")
    aid, r = _submit_and_approve(TestClient(app))
    assert r.json()["crm_update"]["archive"]["status"] == "filed"
    with sqlite3.connect(settings.database_path) as db:
        rows = db.execute("SELECT content FROM mock_archive").fetchall()
    assert len(rows) == 1
    record = json.loads(rows[0][0])
    assert record["approved_by"] == "alice" and record["submitted_by"] and record["submitted_by"] != "alice"
    assert record["approval_note"] == "checked against the form" and record["crm_result"] == "applied"
    again = apply_approved_update(aid, "alice")                       # a retry of the same approval
    assert again["archive"]["status"] == "already_filed"
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM mock_archive").fetchone()[0] == 1
    assert [s for s, _ in _events("document_archived")] == ["filed", "already_filed"]
    assert verify_chain()["ok"] is True


def test_record_content_is_redacted_and_audit_keeps_only_the_item_id(monkeypatch):
    item = {"document_id": "d", "submitted_by": "flow", "decision_note": "SSN 123-45-6789 matched",
            "proposal": {"record_id": "C10482", "proposed_changes": {"email": "a@b.co"}, "crm_current": {}}}
    body = build_record(item, "apr-1", "alice", "applied").decode()
    assert "123-45-6789" not in body and "a@b.co" in body
    monkeypatch.setattr(settings, "archive_provider", "mock")
    _submit_and_approve(TestClient(app))
    assert all(set(json.loads(d)) <= {"approval_id", "provider", "drive_item_id"} for _, d in _events("document_archived"))


def test_real_archive_respects_the_write_gate(monkeypatch):
    monkeypatch.setattr(settings, "archive_provider", "sharepoint")
    monkeypatch.setattr(settings, "write_mode", "shadow")
    monkeypatch.setattr(mg, "get_archive", lambda: pytest.fail("shadow mode must not even build the SharePoint client"))
    _, r = _submit_and_approve(TestClient(app))
    assert r.json()["crm_update"]["archive"] == {"status": "shadow"}
    assert [s for s, _ in _events("archive_shadowed")] == ["would_file"] and _events("document_archived") == []


def test_real_archive_files_when_writes_are_enabled(monkeypatch):
    calls = []

    class Spy:
        def archive_record(self, document_id, discriminator, content):
            calls.append((document_id, discriminator))
            return mg.ArchiveReceipt("SP-ITEM-9")
    monkeypatch.setattr(settings, "archive_provider", "sharepoint")
    monkeypatch.setattr(settings, "write_mode", "enabled")
    from org_automation.workflows import archive_record as ar
    monkeypatch.setattr(ar, "get_archive", lambda: Spy())
    aid, r = _submit_and_approve(TestClient(app))
    assert calls == [("d1", aid)] and r.json()["crm_update"]["archive"]["drive_item_id"] == "SP-ITEM-9"


def test_filing_failure_never_undoes_the_approval_or_the_crm_change(monkeypatch):
    class Down:
        def archive_record(self, *a):
            raise mg.GraphUnavailable("Graph archive upload unavailable (HTTP 503).")
    monkeypatch.setattr(settings, "archive_provider", "sharepoint")
    monkeypatch.setattr(settings, "write_mode", "enabled")
    from org_automation.workflows import archive_record as ar
    monkeypatch.setattr(ar, "get_archive", lambda: Down())
    client = TestClient(app)
    _aid, r = _submit_and_approve(client)                               # still a 200: the approval stands
    assert r.json()["crm_update"]["archive"] == {"status": "failed", "error": "GraphUnavailable"}
    assert client.get("/documents/d1/workflow-status", headers=ALICE).json()["state"] == "completed_applied"
    assert [s for s, _ in _events("archive_failed")] == ["error"]


def test_health_archive_reports_safe_status(monkeypatch):
    client = TestClient(app)
    assert client.get("/health/archive", headers=ALICE).json()["ok"] is None
    monkeypatch.setattr(settings, "archive_provider", "mock")
    assert client.get("/health/archive", headers=ALICE).json() == {"provider": "mock", "write_mode": "enabled", "ok": True}
    assert client.get("/health/archive", headers=SVC).status_code == 403          # reviewers only


def test_the_agent_has_no_way_to_file_or_trigger_archiving():
    from org_automation.agent.tools import TOOLS
    assert not [n for n in TOOLS if "archive" in n or "sharepoint" in n or "graph" in n]
