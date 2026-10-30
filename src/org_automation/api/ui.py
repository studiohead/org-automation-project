"""PLAIN ENGLISH: The little settings page the demo web screen asks for when it opens.

`GET /ui/config` tells the screen (1) which "mode" the app is running in, so a demo can say so out loud, and
(2) in DEVELOPMENT ONLY, which demo logins exist so the screen can show one-click sign-in buttons.
Only logins whose secret starts with "demo-" are ever listed, so a real key can't leak through this page.
In production the list is always empty and people must paste their own key (or, better, use single sign-on).
"""
import json
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr

from org_automation.api.routes import EITHER, REVIEWER, SERVICE, accept_docusign_event
from org_automation.audit.writer import write_event
from org_automation.core.auth import Principal, demo_logins
from org_automation.core.settings import settings
from org_automation.integrations.crm import MockCrmAdapter
from org_automation.integrations.docusign import sign_hmac

ui_router = APIRouter()


def _auth_block() -> dict:
    """What the browser needs to start a Microsoft sign-in. Only public identifiers: no secrets are involved in this flow."""
    block: dict = {"mode": settings.auth_mode, "entra": None}
    if settings.auth_mode != "api_key" and settings.entra_spa_client_id:
        block["entra"] = {"tenant_id": settings.entra_tenant_id, "client_id": settings.entra_spa_client_id,
                          "authority": f"https://login.microsoftonline.com/{settings.entra_tenant_id}",
                          "api_scope": f"api://{settings.entra_api_client_id}/{settings.entra_required_scope}"}
    return block


@ui_router.get("/ui/config", include_in_schema=False)
def ui_config():
    """Open to anyone (the sign-in page needs it), so outside development it says only what the page must know."""
    modes = {"reader": "llm" if settings.extraction_mode == "claude" else settings.extraction_mode,
             "agent": "llm" if settings.agent_mode in ("llm", "claude") else "scripted",
             "crm": settings.crm_provider, "writes": settings.effective_write_mode()}
    if settings.environment != "development":
        return {"app_name": settings.app_name, "environment": settings.environment, "modes": modes, "auth": _auth_block(),
                "demo_logins": [], "llm": {"provider": settings.llm_provider, "model": "", "base_url": "",
                                           "embedding_mode": settings.embedding_mode, "embedding_base_url": "",
                                           "embedding_model": ""}}
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "modes": modes,
        "auth": _auth_block(),
        "demo_logins": demo_logins(),
        "llm": {"provider": settings.llm_provider, "model": settings.claude_model if settings.llm_provider == "claude" else settings.llm_model,
                "base_url": settings.llm_base_url if settings.llm_provider != "claude" else "",
                "embedding_mode": settings.embedding_mode, "embedding_base_url": settings.embedding_base_url,
                "embedding_model": settings.embedding_model},
    }


class LlmSettingsRequest(BaseModel):
    provider: Literal["claude", "openai-compatible"]
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="http://localhost:11434/v1", max_length=500)
    api_key: str = Field(default="", max_length=2000)
    embedding_mode: Literal["none", "ollama"] = "none"
    embedding_model: str = Field(default="nomic-embed-text", min_length=1, max_length=200)
    embedding_base_url: str = Field(default="http://localhost:11434", max_length=500)


@ui_router.post("/ui/settings/llm", include_in_schema=False)
def update_llm_settings(body: LlmSettingsRequest, who: Principal = EITHER):
    """Development-only, in-memory provider switch. Credentials are never written to disk or returned."""
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="Runtime model settings are available only in development.")
    if body.provider == "openai-compatible":
        from urllib.parse import urlparse
        parsed = urlparse(body.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(status_code=422, detail="Enter a valid HTTP(S) model endpoint URL.")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "host.docker.internal"):
            raise HTTPException(status_code=422, detail="Non-local model endpoints must use HTTPS.")
        settings.llm_base_url = body.base_url.rstrip("/")
        settings.llm_model = body.model
        settings.llm_api_key = SecretStr(body.api_key) if body.api_key else None
        settings.llm_provider = "openai-compatible"
    else:
        if not (body.api_key or settings.anthropic_api_key):
            raise HTTPException(status_code=422, detail="Claude requires an Anthropic API key.")   # checked first: a refusal changes nothing
        settings.claude_model = body.model
        if body.api_key:
            settings.anthropic_api_key = SecretStr(body.api_key)
        settings.llm_provider = "claude"
    settings.extraction_mode = "claude"
    settings.agent_mode = "llm"
    # Embedding settings are updated only by /ui/settings/embeddings; saving the generation model
    # must not overwrite an independently configured embedding model.
    write_event("llm_settings_changed", "runtime-config", "updated",
                {"provider": settings.llm_provider, "model": body.model, "embedding_mode": settings.embedding_mode,
                 "embedding_model": settings.embedding_model, "credential_saved": False}, actor=who.name)
    return {"ok": True, "provider": settings.llm_provider, "model": body.model, "agent_mode": "llm",
            "embedding_mode": settings.embedding_mode, "embedding_model": settings.embedding_model,
            "embedding_base_url": settings.embedding_base_url,
            "note": "Applied in this running process only; credentials are memory-only and will be lost on restart."}


class EmbeddingSettingsRequest(BaseModel):
    embedding_mode: Literal["none", "ollama"] = "none"
    embedding_model: str = Field(default="nomic-embed-text", min_length=1, max_length=200)
    embedding_base_url: str = Field(default="http://localhost:11434", max_length=500)


@ui_router.post("/ui/settings/embeddings", include_in_schema=False)
def update_embedding_settings(body: EmbeddingSettingsRequest, who: Principal = EITHER):
    """Update only embedding settings; never re-submit or mutate generation-model settings."""
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="Runtime model settings are available only in development.")
    from urllib.parse import urlparse
    parsed = urlparse(body.embedding_base_url)
    if body.embedding_mode == "ollama":
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(status_code=422, detail="Enter a valid HTTP(S) embedding endpoint URL.")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "host.docker.internal"):
            raise HTTPException(status_code=422, detail="Non-local embedding endpoints must use HTTPS.")
    settings.embedding_mode = body.embedding_mode
    settings.embedding_model = body.embedding_model.strip()
    settings.embedding_base_url = body.embedding_base_url.rstrip("/")
    write_event("embedding_settings_changed", "runtime-config", "updated",
                {"embedding_mode": settings.embedding_mode, "embedding_model": settings.embedding_model,
                 "embedding_base_url": settings.embedding_base_url}, actor=who.name)
    return {"ok": True, "embedding_mode": settings.embedding_mode,
            "embedding_model": settings.embedding_model, "embedding_base_url": settings.embedding_base_url,
            "note": "Embedding settings applied independently; generation model settings were not changed."}


class ModelTestRequest(BaseModel):
    target: Literal["generation", "embedding"]


@ui_router.post("/ui/settings/reindex-embeddings", include_in_schema=False)
def reindex_embeddings(who: Principal = EITHER):
    """Create vectors for all already-indexed policy chunks using the current in-memory embedding settings."""
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="Embedding re-indexing is available only in development.")
    if settings.embedding_mode != "ollama":
        raise HTTPException(status_code=422, detail="Enable Ollama embeddings and apply settings first.")
    import json

    from org_automation.knowledge.embeddings import embed_texts
    from org_automation.storage.database import connect
    with connect() as db:
        rows = db.execute(
            "SELECT c.child_id,c.document_id,c.text,d.title,p.section_title FROM kb_children c "
            "JOIN knowledge_documents d ON d.document_id=c.document_id "
            "JOIN kb_parents p ON p.parent_id=c.parent_id ORDER BY c.document_id,c.ordinal"
        ).fetchall()
    if not rows:
        return {"ok": True, "documents": 0, "chunks": 0, "model": settings.embedding_model,
                "message": "No policy chunks are indexed yet. Add/index policy documents first."}
    inputs = [f"{row['title']}\n{row['section_title']}\n{row['text']}" for row in rows]
    try:
        vectors = embed_texts(inputs)
        if len(vectors) != len(rows):
            raise RuntimeError("Embedding count did not match chunk count.")
        with connect() as db:
            db.executemany(
                "INSERT INTO kb_embeddings(child_id,model,vector_json) VALUES (?,?,?) "
                "ON CONFLICT(child_id) DO UPDATE SET model=excluded.model, vector_json=excluded.vector_json",
                [(row["child_id"], settings.embedding_model, json.dumps(vector, separators=(",", ":")))
                 for row, vector in zip(rows, vectors)]
            )
        doc_count = len({row["document_id"] for row in rows})
        write_event("kb_embeddings_reindexed", "policy-library", "indexed",
                    {"model": settings.embedding_model, "chunks": len(rows)}, actor=who.name)
        return {"ok": True, "documents": doc_count, "chunks": len(rows), "model": settings.embedding_model,
                "dimensions": len(vectors[0]) if vectors else 0,
                "message": "Indexed policy chunks now have vectors for semantic search."}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding re-index failed ({type(exc).__name__}). Check the embedding endpoint and model.") from exc


@ui_router.post("/ui/settings/test", include_in_schema=False)
def test_model_connection(body: ModelTestRequest, who: Principal = EITHER):
    """Make a real, minimal request to the configured model. Never returns prompts, document text, or credentials."""
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="Model connection tests are available only in development.")
    try:
        if body.target == "embedding":
            if settings.embedding_mode != "ollama":
                raise HTTPException(status_code=422, detail="Choose Ollama embeddings and apply settings first.")
            from org_automation.knowledge.embeddings import embed_texts
            vectors = embed_texts(["Connection test for semantic search."])
            if not vectors or not vectors[0]:
                raise RuntimeError("The embedding service returned no vector.")
            return {"ok": True, "target": "embedding", "model": settings.embedding_model,
                    "dimensions": len(vectors[0]), "message": "Embedding request succeeded."}
        from org_automation.ai.client import LlmClient
        fields = ["client_id", "effective_date", "email"]
        result = LlmClient().extract_structured(
            pages=["Client ID: TEST-123\nEffective date: 2026-10-21\nEmail: test@example.com"],
            field_names=fields)
        missing = [name for name in fields if name not in result]
        if missing:
            raise RuntimeError("The generation model did not return the required structured fields.")
        return {"ok": True, "target": "generation", "provider": settings.llm_provider,
                "model": settings.claude_model if settings.llm_provider == "claude" else settings.llm_model,
                "message": "Generation request succeeded and returned structured extraction fields."}
    except HTTPException:
        raise
    except Exception as exc:
        # Safe diagnostic: exception type only, never provider response bodies or credentials.
        raise HTTPException(status_code=502, detail=f"{body.target.title()} connection failed ({type(exc).__name__}). Check endpoint, model name, and server logs.") from exc


class WebhookDemo(BaseModel):
    envelope_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    event: Literal["envelope-sent", "envelope-completed"] = "envelope-completed"
    forge: bool = False      # sign with the wrong secret, like an impostor would


@ui_router.post("/ui/demo/webhook", include_in_schema=False)
def demo_webhook(body: WebhookDemo, who: Principal = SERVICE):
    """DEVELOPMENT DEMO ONLY. The server plays DocuSign: it builds a "document changed" message, signs it with the shared
    secret (the browser never sees the secret) and hands it to the REAL webhook receiver, then reports what the receiver
    said. A forged message is signed with a wrong secret, so it must be rejected."""
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="The webhook demo is available only in development.")
    secret = settings.docusign_hmac_secret.get_secret_value() if settings.docusign_hmac_secret else ""
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook not configured (set DOCUSIGN_HMAC_SECRET).")
    payload = {"event": body.event, "apiVersion": "v2.1", "data": {"envelopeId": body.envelope_id}}
    raw = json.dumps(payload).encode()
    signature = sign_hmac("not-the-real-secret" if body.forge else secret, raw)
    sent = {"header": "X-DocuSign-Signature-1", "signature_start": signature[:14], "body": payload, "forged": body.forge}
    try:
        return {"sent": sent, "received": {"http_status": 202, "body": accept_docusign_event(raw, signature, None)}}
    except HTTPException as exc:
        return {"sent": sent, "received": {"http_status": exc.status_code, "body": {"detail": exc.detail}}}


class StaffEdit(BaseModel):
    record_id: Literal["C10482", "C10001", "C20005"]
    email: str = Field(max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    effective_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@ui_router.post("/ui/demo/staff-edit", include_in_schema=False)
def demo_staff_edit(body: StaffEdit, who: Principal = REVIEWER):
    """DEVELOPMENT DEMO ONLY, pretend CRM only. Stands in for a colleague editing the client in Salesforce, so the scorecard
    has something to compare against. It deliberately bypasses the write gate (it is not this system writing) and says so in the log."""
    if settings.environment != "development" or settings.crm_provider != "mock":
        raise HTTPException(status_code=403, detail="The staff-edit helper exists only in development with the pretend CRM.")
    MockCrmAdapter().update_record(body.record_id, {"email": body.email, "effective_date": body.effective_date}, f"staff-edit:{uuid.uuid4()}")
    write_event("demo_staff_edit", "n/a", "simulated", {"record_id": body.record_id, "fields": ["effective_date", "email"]}, actor=who.name)
    return {"ok": True, "record_id": body.record_id}
