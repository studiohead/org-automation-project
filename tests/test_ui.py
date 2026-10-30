"""PLAIN ENGLISH: Checks for the demo web screen and the few endpoints it relies on: the page is served with strict
browser rules, demo sign-in buttons appear only for demo keys in development, and the audit/history endpoints
are reviewer-only."""
from fastapi.testclient import TestClient

from org_automation.core.settings import settings
from org_automation.main import app

client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}
GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: new@example.com"


def submit(event_id="evt-ui-1", doc="doc-ui-1"):
    body = {"document_type": "client_update", "event_id": event_id, "text": GOOD}
    return client.post(f"/documents/{doc}/process", json=body, headers=SVC).json()


def test_page_and_assets_are_served_with_strict_headers():
    page = client.get("/ui/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'id="app"' in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/ui/app.js").status_code == 200 and client.get("/ui/styles.css").status_code == 200


def test_root_redirects_to_the_screen():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"


def test_the_screen_never_inserts_server_text_as_html():
    js = client.get("/ui/app.js").text
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js


def test_demo_logins_only_for_demo_keys_in_development(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", "flow:demo-service-key:service,alice:real-secret-1:reviewer")
    monkeypatch.setattr(settings, "environment", "development")
    cfg = client.get("/ui/config")
    assert [p["name"] for p in cfg.json()["demo_logins"]] == ["flow"]
    assert "real-secret-1" not in cfg.text
    monkeypatch.setattr(settings, "environment", "production")
    assert client.get("/ui/config").json()["demo_logins"] == []


def test_me_reports_name_and_role_and_needs_a_valid_key():
    assert client.get("/me", headers=ALICE).json() == {"name": "alice", "role": "reviewer", "auth": "api_key"}
    assert client.get("/me", headers=SVC).json() == {"name": "flow", "role": "service", "auth": "api_key"}
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"X-API-Key": "nope"}).status_code == 401


def test_audit_events_are_reviewer_only_newest_first_and_filterable():
    submit()
    submit(event_id="evt-ui-2", doc="doc-ui-2")
    assert client.get("/audit/events", headers=SVC).status_code == 403
    items = client.get("/audit/events", headers=ALICE).json()["items"]
    assert [i["seq"] for i in items] == sorted((i["seq"] for i in items), reverse=True)
    assert {i["event_type"] for i in items} == {"document_processed"}
    only = client.get("/audit/events?document_id=doc-ui-2", headers=ALICE).json()["items"]
    assert [i["document_id"] for i in only] == ["doc-ui-2"] and isinstance(only[0]["details"], dict)
    assert client.get("/audit/events?limit=0", headers=ALICE).status_code == 422


def test_approvals_can_be_listed_by_status():
    approval_id = submit()["approval_id"]
    assert len(client.get("/approvals", headers=ALICE).json()["items"]) == 1
    assert client.get("/approvals?status=approved", headers=ALICE).json()["items"] == []
    client.post(f"/approvals/{approval_id}/decision", json={"decision": "approve", "note": "ok"}, headers=ALICE)
    assert client.get("/approvals", headers=ALICE).json()["items"] == []
    done = client.get("/approvals?status=approved", headers=ALICE).json()["items"]
    assert [i["approval_id"] for i in done] == [approval_id]
    assert [i["approval_id"] for i in client.get("/approvals?status=all", headers=ALICE).json()["items"]] == [approval_id]
    assert client.get("/approvals?status=bogus", headers=ALICE).status_code == 422
    assert client.get("/approvals?status=all", headers=SVC).status_code == 403


def test_agent_run_names_its_driver_and_pairs_each_request_with_the_gateway_decision():
    r = client.post("/agent/runs/env-1001", json={"event_id": "run-driver"}, headers=SVC).json()
    assert r["driver"] == "scripted" and r["model"] is None and r["model_notes"] == []
    first = r["tool_calls"][0]
    assert first["step"] == 1 and first["tool"] == "get_document_status" and first["args"] == {"document_id": "env-1001"}
    assert first["outcome"] == "ok" and first["ok"] is True


def test_claude_driven_run_records_model_text_and_refused_requests():
    from test_agent import ATTACK_AND_WORK, ClaudeAgent, HijackedLlm

    from org_automation.agent.runner import run_agent
    r = run_agent("env-1004", "drv-claude", "flow", agent=ClaudeAgent(HijackedLlm(ATTACK_AND_WORK)))
    assert r["driver"] == "llm" and r["model"] == settings.claude_model and r["model_notes"] == ["done"]
    bad = [t for t in r["tool_calls"] if not t["ok"]]
    assert {t["tool"] for t in bad} == {"apply_approved_update", "write_audit_event", "get_client_record"}
    assert all(t["outcome"] in {"denied", "unknown_tool"} and t["why"] for t in bad)
    assert next(t for t in bad if t["tool"] == "get_client_record")["args"] == {"client_id": "C10001"}



def test_real_llm_attack_test_forces_configured_llm_even_when_agent_mode_is_scripted(monkeypatch):
    from org_automation.api import routes
    sentinel = object()
    captured = {}
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(routes, "LlmAgent", lambda: sentinel)

    def fake_run_agent(document_id, event_id, actor, resume_approval_id=None, agent=None):
        captured.update(document_id=document_id, agent=agent)
        return {"driver": "llm", "model": settings.llm_model, "tool_calls": [], "model_notes": []}

    monkeypatch.setattr(routes, "run_agent", fake_run_agent)
    response = client.post("/agent/runs/env-1004", json={"event_id": "real-llm-attack", "use_real_llm": True}, headers=SVC)
    assert response.status_code == 200
    assert captured == {"document_id": "env-1004", "agent": sentinel}
    assert client.post("/agent/runs/env-1001", json={"event_id": "real-llm-wrong-doc", "use_real_llm": True}, headers=SVC).status_code == 422
    assert client.post("/agent/runs/env-1004", json={"event_id": "both-modes", "use_real_llm": True, "simulate_attack": True}, headers=SVC).status_code == 422


def test_attack_simulation_is_labelled_contained_and_dev_only(monkeypatch):
    from org_automation.integrations.crm import MockCrmAdapter
    r = client.post("/agent/runs/env-1004", json={"event_id": "sim-1", "simulate_attack": True}, headers=SVC).json()
    assert r["driver"] == "simulated" and r["model"] is None and "SIMULATION" in r["model_notes"][0]
    assert {t["tool"] for t in r["tool_calls"] if not t["ok"]} == {"apply_approved_update", "write_audit_event", "get_client_record"}
    assert r["status"] == "awaiting_review"
    assert MockCrmAdapter().get_record("C10001")["email"] == "two@example.com"      # the attack changed nothing
    assert client.get("/audit/verify", headers=ALICE).json()["ok"] is True
    # only on the document it was written for, and only in development
    assert client.post("/agent/runs/env-1001", json={"event_id": "sim-2", "simulate_attack": True}, headers=SVC).status_code == 422
    monkeypatch.setattr(settings, "environment", "production")
    assert client.post("/agent/runs/env-1004", json={"event_id": "sim-3", "simulate_attack": True}, headers=SVC).status_code == 403


def test_llm_settings_visible_in_development_for_service_and_reviewer_roles(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    js = client.get("/ui/app.js").text
    assert "config.environment === 'development' ? [{ id: 'settings', label: 'LLM settings'" in js
    # The service account can configure the provider too; this endpoint remains development-only.
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "llm_model", "llama3.1")
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "extraction_mode", "demo")
    monkeypatch.setattr(settings, "agent_mode", "scripted")
    response = client.post("/ui/settings/llm", json={
        "provider": "openai-compatible",
        "model": "llama3.1",
        "base_url": "http://localhost:11434/v1",
        "api_key": "",
    }, headers=SVC)
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "openai-compatible"
    assert response.json()["agent_mode"] == "llm"
    assert settings.agent_mode == "llm"


def test_llm_settings_keep_generation_and_embedding_models_separate(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    response = client.post("/ui/settings/llm", json={
        "provider": "openai-compatible", "model": "llama3.1",
        "base_url": "http://localhost:11434/v1", "api_key": "",
        "embedding_mode": "ollama", "embedding_model": "nomic-embed-text",
        "embedding_base_url": "http://localhost:11434",
    }, headers=SVC)
    assert response.status_code == 200, response.text
    assert settings.llm_model == "llama3.1"
    # Generation save must not apply or overwrite embedding settings, even if legacy
    # clients include embedding fields in this request.
    assert settings.embedding_model == "nomic-embed-text"
    assert settings.embedding_mode == "none"
    cfg = client.get("/ui/config").json()["llm"]
    assert cfg["model"] == "llama3.1"
    assert cfg["embedding_model"] == "nomic-embed-text"
    assert cfg["embedding_mode"] == "none"


def test_generation_connection_test_uses_configured_llm(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    class FakeLlm:
        def extract_structured(self, *, pages, field_names):
            assert "Client ID: TEST-123" in pages[0]
            return {name: {"value": None} for name in field_names}
    from org_automation.ai.client import LlmClient
    monkeypatch.setattr(LlmClient, "__new__", lambda cls: FakeLlm())
    response = client.post("/ui/settings/test", json={"target": "generation"}, headers=SVC)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_embedding_connection_test_calls_embedding_provider(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "embedding_mode", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "nomic-embed-text")
    from org_automation.knowledge import embeddings
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [[0.1, 0.2, 0.3]])
    response = client.post("/ui/settings/test", json={"target": "embedding"}, headers=SVC)
    assert response.status_code == 200, response.text
    assert response.json()["dimensions"] == 3


def test_reindex_embeddings_uses_runtime_embedding_settings(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "embedding_mode", "none")
    from org_automation.workflows.index_policy import index_policy_document
    index_policy_document("reindex-test", "Reindex test", "all_staff", "# Example\nA signed form needs review.")
    monkeypatch.setattr(settings, "embedding_mode", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "nomic-embed-text")
    from org_automation.knowledge import embeddings
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])
    response = client.post("/ui/settings/reindex-embeddings", json={}, headers=ALICE)
    assert response.status_code == 200, response.text
    assert response.json()["chunks"] >= 1
    assert response.json()["dimensions"] == 2


def test_process_document_shows_wait_message_while_request_is_pending():
    js = client.get("/ui/app.js").text
    assert "button.textContent = 'Processing…'" in js
    assert "button.setAttribute('aria-busy', 'true')" in js
    assert "Processing document with the configured model. This may take a minute for PDFs; please keep this page open." in js
    assert "button.removeAttribute('aria-busy')" in js


def test_embedding_settings_apply_independently_of_generation(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "llm_provider", "openai-compatible")
    monkeypatch.setattr(settings, "llm_model", "llama3.1")
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "embedding_mode", "none")
    monkeypatch.setattr(settings, "embedding_model", "nomic-embed-text")
    response = client.post("/ui/settings/embeddings", json={
        "embedding_mode": "ollama", "embedding_model": "mxbai-embed-large",
        "embedding_base_url": "http://localhost:11434"
    }, headers=ALICE)
    assert response.status_code == 200
    assert settings.embedding_mode == "ollama"
    assert settings.embedding_model == "mxbai-embed-large"
    assert settings.llm_provider == "openai-compatible"
    assert settings.llm_model == "llama3.1"
    assert "generation model settings were not changed" in response.json()["note"]
