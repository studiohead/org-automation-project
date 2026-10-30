"""PLAIN ENGLISH: The app's front door — every web address (endpoint) it answers, and who may use it.

  GET  /health                       anyone       "are you alive?"
  GET  /me                           either       "who am I and what role does my key have?" (used by the web page)
  GET  /health/crm                   reviewer     "can we reach the CRM (mock or Salesforce) and log in?"
  GET  /health/archive               reviewer     "can we reach the SharePoint archive (or its mock) and log in?"
  POST /documents/{id}/process       service key  submit document TEXT (use \\f between pages)
  POST /documents/{id}/process-pdf   service key  submit a PDF file
  POST /agent/runs/{id}              service key  let the tool-using agent process a signed DocuSign envelope
                                                  (development only: {"simulate_attack": true} replays a pretend attack; {"use_real_llm": true} forces the configured real model on env-1004)
  GET  /documents/{id}/workflow-status  either   where is this document in the workflow?
  GET  /knowledge/search?q=...       either       search the policy library (results limited to what the caller's role may see)
  GET  /knowledge/documents          either       list visible policy documents
  POST /knowledge/documents/{id}     reviewer     add or replace a policy document (re-indexing is safe)
  DELETE /knowledge/documents/{id}   reviewer     remove a policy document and all its search entries
  GET  /approvals                    reviewer     what's waiting for a human (?status=pending|approved|rejected|stale|all)
  POST /approvals/{id}/decision      reviewer     approve / reject (recorded under the reviewer's name)
  GET  /audit/verify                 reviewer     re-check the diary hasn't been tampered with
  GET  /audit/events                 reviewer     the newest diary entries (?limit=, ?document_id=)
  GET  /scorecard                    reviewer     how the automation is doing, incl. agreement with what staff did (?days=)
  POST /webhooks/docusign            DocuSign     signature-verified; duplicates ignored; a signed-and-completed
                                                  envelope then starts an agent run (re-checked with DocuSign itself)
Power Automate can call the two POST /documents endpoints with a plain "HTTP" action.
Interactive docs: http://127.0.0.1:8000/docs
"""
import json
import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from org_automation.agent.runner import LlmAgent, run_agent
from org_automation.agent.simulation import SIMULATED_DOCUMENT, SimulatedHijackedLlm
from org_automation.agent.tools import workflow_state
from org_automation.approvals.service import ApprovalError, list_approvals, list_pending
from org_automation.audit.writer import list_events, verify_chain, write_event
from org_automation.core.auth import Principal, require_role
from org_automation.core.idempotency import claim_event, finish_event, release_event
from org_automation.core.settings import settings
from org_automation.documents.pdf import MAX_BYTES, PdfError, pdf_to_pages
from org_automation.documents.schemas import DecisionRequest
from org_automation.integrations.crm import CrmError, get_crm
from org_automation.integrations.docusign import verify_hmac
from org_automation.integrations.microsoft_graph import get_archive
from org_automation.knowledge import store as kb_store
from org_automation.knowledge.search import search as kb_search
from org_automation.reporting.scorecard import build_scorecard
from org_automation.workflows.apply_update import ApplyError
from org_automation.workflows.index_policy import delete_policy_document, index_policy_document
from org_automation.workflows.process_document import process_document
from org_automation.workflows.review_decision import review

router = APIRouter()
log = logging.getLogger("org")

SERVICE = require_role("service")
REVIEWER = require_role("reviewer")
EITHER = require_role("service", "reviewer")


class ProcessRequest(BaseModel):
    document_type: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=100000)
    event_id: str = Field(min_length=1, max_length=200)


def _run(document_id, document_type, content, event_id, who: Principal):
    try:
        return process_document(document_id, document_type, content, event_id, actor=who.name).model_dump(mode="json")
    except RuntimeError as exc:  # e.g. the AI service is unreachable or misconfigured
        log.error("extraction failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="An upstream service (AI or CRM) is unavailable; safe to retry.") from exc


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/me")
def me(who: Principal = EITHER):
    """Lets the web page learn the caller's name and role from their key (the key itself is never echoed)."""
    return {"name": who.name, "role": who.role, "auth": who.auth}


@router.get("/health/crm")
def health_crm(who: Principal = REVIEWER):
    """Is the configured CRM reachable and are our credentials accepted? (Reports only a safe yes/no and error type.)"""
    provider = settings.crm_provider
    try:
        return {"provider": provider, "write_mode": settings.effective_write_mode(), "ok": bool(get_crm().ping())}
    except Exception as exc:  # noqa: BLE001  (report the type only; never details that could include secrets)
        return {"provider": provider, "ok": False, "error": type(exc).__name__}


@router.get("/health/archive")
def health_archive(who: Principal = REVIEWER):
    """Is the archive (SharePoint, or the local mock) reachable and are our credentials accepted? Safe yes/no only."""
    provider = settings.archive_provider
    if provider == "none":
        return {"provider": provider, "ok": None, "detail": "No archive configured."}
    try:
        return {"provider": provider, "write_mode": "enabled" if provider == "mock" else (settings.write_mode or "shadow"),
                "ok": bool(get_archive().ping())}
    except Exception as exc:  # noqa: BLE001  (type only; never details that could include secrets)
        return {"provider": provider, "ok": False, "error": type(exc).__name__}


@router.post("/documents/{document_id}/process")
def process(document_id: str, request: ProcessRequest, who: Principal = SERVICE):
    return _run(document_id, request.document_type, request.text, request.event_id, who)


@router.post("/documents/{document_id}/process-pdf")
def process_pdf(document_id: str, file: UploadFile = File(...),  # noqa: B008
                 document_type: str = Form(..., max_length=80),
                event_id: str = Form(..., max_length=200), who: Principal = SERVICE):
    data = file.file.read(MAX_BYTES + 1)
    try:
        pages = pdf_to_pages(data)
    except PdfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _run(document_id, document_type, pages, event_id, who)


class AgentRunRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    approval_id: str | None = Field(default=None, max_length=100)   # set ONLY to retry an already-approved update
    simulate_attack: bool = False    # development demo only: replay a pretend hijacked agent (see agent/simulation.py)
    use_real_llm: bool = False       # development demo only: force configured real LLM regardless of AGENT_MODE


@router.post("/agent/runs/{document_id}")
def agent_run(document_id: str, body: AgentRunRequest, who: Principal = SERVICE):
    agent = None
    if body.simulate_attack and body.use_real_llm:
        raise HTTPException(status_code=422, detail="Choose either the simulated attack or the real LLM test, not both.")
    if body.use_real_llm:
        if settings.environment != "development":
            raise HTTPException(status_code=403, detail="The real-LLM attack test is available only in development.")
        if document_id != SIMULATED_DOCUMENT or body.approval_id:
            raise HTTPException(status_code=422, detail=f"The real-LLM attack test only runs on {SIMULATED_DOCUMENT} and cannot resume approvals.")
        # Explicitly use the configured provider/model. This intentionally overrides AGENT_MODE
        # for this development-only test, so AGENT_MODE=scripted cannot silently run a checklist.
        agent = LlmAgent()
    if body.simulate_attack:
        if settings.environment != "development":
            raise HTTPException(status_code=403, detail="The attack simulation is available only in development.")
        if document_id != SIMULATED_DOCUMENT or body.approval_id:
            raise HTTPException(status_code=422, detail=f"The attack simulation only runs on {SIMULATED_DOCUMENT}.")
        agent = LlmAgent(SimulatedHijackedLlm())
    try:
        return run_agent(document_id, body.event_id, who.name, resume_approval_id=body.approval_id, agent=agent)
    except RuntimeError as exc:
        log.error("agent run failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Agent unavailable; safe to retry.") from exc


@router.get("/documents/{document_id}/workflow-status")
def workflow_status(document_id: str, who: Principal = EITHER):
    return workflow_state(document_id)


# What each role may see in the policy library. (Production: derive from the user's directory groups.)
ROLE_AUDIENCES = {"service": frozenset({"all_staff"}), "reviewer": frozenset({"all_staff", "advisors", "compliance"})}


class PolicyDocRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    audience: Literal["all_staff", "advisors", "compliance"]
    text: str = Field(min_length=1, max_length=200000)


@router.get("/knowledge/search")
def knowledge_search(q: str = Query(min_length=3, max_length=200), top_k: int = Query(default=3, ge=1, le=5),
                     who: Principal = EITHER):
    results = kb_search(q, ROLE_AUDIENCES[who.role], top_k)
    write_event("kb_search", "policy-library", "retrieved",
                {"result_count": len(results), "sources": [r["citation"]["document_id"] + ":" + r["citation"]["parent_id"] for r in results],
                 "methods": [r["retrieval"]["method"] for r in results]}, actor=who.name)
    return {"results": results}


@router.get("/knowledge/answer")
def knowledge_answer(q: str = Query(min_length=3, max_length=200), top_k: int = Query(default=5, ge=1, le=5),
                    who: Principal = EITHER):
    """Retrieve across the caller's authorized indexed corpus, then synthesize an answer with source citations."""
    results = kb_search(q, ROLE_AUDIENCES[who.role], top_k)
    if not results:
        write_event("kb_answer", "policy-library", "no_match", {"result_count": 0}, actor=who.name)
        return {"answer": "I couldn't find a matching passage in the indexed documents. Try different wording or index the relevant document first.",
                "results": [], "model_used": False}
    try:
        from org_automation.ai.client import LlmClient
        answer = LlmClient().answer_question(question=q, sources=results)
        write_event("kb_answer", "policy-library", "answered",
                    {"result_count": len(results), "sources": [r["citation"]["document_id"] + ":" + r["citation"]["parent_id"] for r in results],
                     "provider": settings.llm_provider, "model": settings.claude_model if settings.llm_provider == "claude" else settings.llm_model}, actor=who.name)
    except Exception as exc:
        log.error("knowledge answer failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="The answer model is unavailable; retrieved source passages are still available from /knowledge/search.") from exc
    return {"answer": answer, "results": results, "model_used": True}


@router.get("/knowledge/documents")
def knowledge_documents(who: Principal = EITHER):
    return {"items": kb_store.list_documents(ROLE_AUDIENCES[who.role])}


@router.post("/knowledge/documents/upload")
def knowledge_upload_pdf(
    file: UploadFile = File(...),  # noqa: B008
    document_id: str = Form(..., min_length=1, max_length=100),
    title: str = Form(..., min_length=1, max_length=200),
    audience: Literal["all_staff", "advisors", "compliance"] = Form("all_staff"),
    who: Principal = REVIEWER,
):
    """Index a text-based PDF into the searchable policy library. This is separate from client-change processing."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Upload a PDF file. Text extraction from scanned PDFs requires OCR, which is not enabled.")
    data = file.file.read(MAX_BYTES + 1)
    try:
        pages = pdf_to_pages(data)
    except PdfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    text = "\n\n".join(f"## Page {i}\n{page}" for i, page in enumerate(pages, start=1) if page.strip())
    try:
        result = index_policy_document(document_id, title, audience, text, actor=who.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**result, "filename": file.filename, "pages": len(pages),
            "note": "Indexed in the policy library. This does not query or index Salesforce or SharePoint."}


@router.post("/knowledge/documents/{document_id}")
def knowledge_index(document_id: str, body: PolicyDocRequest, who: Principal = REVIEWER):
    try:
        return index_policy_document(document_id, body.title, body.audience, body.text, actor=who.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/knowledge/documents/{document_id}")
def knowledge_delete(document_id: str, who: Principal = REVIEWER):
    try:
        existed = delete_policy_document(document_id, actor=who.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not existed:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"document_id": document_id, "status": "deleted"}


@router.get("/approvals")
def approvals(status: Literal["pending", "approved", "rejected", "stale", "all"] = "pending",
              limit: int = Query(default=50, ge=1, le=200), who: Principal = REVIEWER):
    if status == "pending":
        return {"items": list_pending()}          # unchanged behaviour: oldest first, nothing hidden
    return {"items": list_approvals(status, limit)}


@router.post("/approvals/{approval_id}/decision")
def decision(approval_id: str, body: DecisionRequest, who: Principal = REVIEWER):
    try:
        return review(approval_id, who.name, body.decision, body.note)
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ApplyError as exc:   # decision is recorded, but the CRM write was refused (e.g. record changed since review)
        raise HTTPException(status_code=409, detail=f"Decision recorded, but not applied: {exc}") from exc
    except CrmError as exc:
        raise HTTPException(status_code=502, detail="Decision recorded, but the CRM is unavailable; retry the apply later.") from exc


@router.get("/audit/verify")
def audit_verify(who: Principal = REVIEWER):
    return verify_chain()


@router.get("/scorecard")
def scorecard(days: int = Query(default=30, ge=1, le=365), who: Principal = REVIEWER):
    return build_scorecard(days)


@router.get("/audit/events")
def audit_events(limit: int = Query(default=50, ge=1, le=200), document_id: str | None = Query(default=None, max_length=100),
                 who: Principal = REVIEWER):
    return {"items": list_events(limit, document_id)}


WEBHOOK_ACTOR = "docusign"
RUN_ON_EVENTS = frozenset({"envelope-completed"})   # only "the document is fully signed" starts work


def _run_webhook_agent(key: str, envelope_id: str, event: str) -> dict:
    """Process a signed envelope after its webhook arrived. The agent re-checks signing status with DocuSign itself
    and never trusts the webhook's claim. On a crash the claim is released so DocuSign's retry can try again."""
    try:
        result = run_agent(envelope_id, f"webhook:{event}:{envelope_id}", WEBHOOK_ACTOR)
        finish_event(key, result["status"])
        return result
    except Exception as exc:  # noqa: BLE001  (log the type only; never details that could include secrets)
        write_event("webhook_processing_failed", envelope_id, "error", {"event": event, "error": type(exc).__name__},
                    actor=WEBHOOK_ACTOR)
        release_event(key)
        return {"status": "failed", "error": type(exc).__name__}


def accept_docusign_event(body: bytes, signature: str | None, background: BackgroundTasks | None) -> dict:
    """The whole webhook receiver. With `background` the agent runs after the 202 is sent (real use). With None it
    runs right now and its result is returned (demo only), so a screen can show what happened."""
    secret = settings.docusign_hmac_secret.get_secret_value() if settings.docusign_hmac_secret else ""
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook not configured.")
    if len(body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Payload too large.")
    if not verify_hmac(secret, body, signature):
        raise HTTPException(status_code=401, detail="Bad signature.")
    try:
        payload = json.loads(body)
        envelope_id = str(payload["data"]["envelopeId"])
        event = str(payload.get("event", "unknown"))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Unrecognized payload.") from exc
    key, won = claim_event("docusign", f"{event}:{envelope_id}", envelope_id)
    if not won:
        write_event("duplicate_webhook", envelope_id, "ignored", {"event": event}, actor=WEBHOOK_ACTOR)
        return {"status": "duplicate_ignored"}
    write_event("webhook_received", envelope_id, "accepted", {"event": event}, actor=WEBHOOK_ACTOR)
    if event not in RUN_ON_EVENTS:
        finish_event(key, "logged_only")          # e.g. "sent" or "delivered": worth recording, nothing to process yet
        return {"status": "accepted", "agent_run": "not_needed"}
    if background is not None:
        background.add_task(_run_webhook_agent, key, envelope_id, event)
        return {"status": "accepted", "agent_run": "queued"}
    return {"status": "accepted", "agent_run": _run_webhook_agent(key, envelope_id, event)}


@router.post("/webhooks/docusign", status_code=202)
async def docusign_webhook(request: Request, background: BackgroundTasks):
    return accept_docusign_event(await request.body(), request.headers.get("X-DocuSign-Signature-1"), background)
