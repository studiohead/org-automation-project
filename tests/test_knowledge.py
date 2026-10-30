"""PLAIN ENGLISH: Tests for the policy library: slicing documents into parent/child pieces, safe re-indexing,
search with citations, hiding restricted policies from people who shouldn't see them, deletion, and the AI tool."""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from org_automation.agent import gateway
from org_automation.agent.runner import run_agent
from org_automation.approvals.service import list_pending
from org_automation.core.settings import settings
from org_automation.documents.chunking import MAX_CHILD_CHARS, MAX_PARENT_CHARS, chunk_document
from org_automation.knowledge.loader import load_policy_dir
from org_automation.knowledge.search import build_match, search
from org_automation.main import app
from org_automation.workflows.index_policy import delete_policy_document, index_policy_document

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"
ALL, STAFF = frozenset({"all_staff", "advisors", "compliance"}), frozenset({"all_staff"})
DOC = "# Alpha\nFirst paragraph about widgets.\n\nSecond paragraph about gadgets.\n\n# Beta\nBeta text about sprockets."


def load_samples():
    for d in load_policy_dir(POLICIES):
        index_policy_document(d["document_id"], d["title"], d["audience"], d["text"])


def count(table):
    with sqlite3.connect(settings.database_path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------------ chunking
def test_children_point_to_their_parents_with_stable_ids():
    parents, children = chunk_document("doc", DOC)
    assert [p.section_title for p in parents] == ["Alpha", "Beta"]
    assert {c.parent_id for c in children} <= {p.parent_id for p in parents}
    assert (parents, children) == chunk_document("doc", DOC)            # deterministic: no random IDs
    assert parents[0].parent_id == "doc:p1" and children[0].child_id == "doc:p1:c1"


def test_size_caps_hold_and_nothing_is_lost():
    big = "# Big\n" + "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(20))
    parents, children = chunk_document("doc", big)
    assert len(parents) > 1 and all(len(p.text) <= MAX_PARENT_CHARS for p in parents)
    assert all(len(c.text) <= MAX_CHILD_CHARS for c in children)
    assert "Paragraph 19." in " ".join(c.text for c in children)
    giant = "# One\n" + "x" * 5000                                        # a single enormous "sentence"
    _, kids = chunk_document("doc", giant)
    assert all(len(k.text) <= MAX_CHILD_CHARS for k in kids) and sum(len(k.text) for k in kids) == 5000


def test_text_without_headings_is_one_section_and_bad_ids_rejected():
    parents, _ = chunk_document("doc", "Just some plain text.")
    assert [p.section_title for p in parents] == ["Introduction"]
    with pytest.raises(ValueError):
        chunk_document("../evil", DOC)


# ------------------------------------------------------------------ indexing
def test_reindexing_is_idempotent_and_edits_replace_not_duplicate():
    assert index_policy_document("doc", "Doc", "all_staff", DOC)["status"] == "indexed"
    before = (count("kb_parents"), count("kb_children"), count("kb_children_fts"))
    assert index_policy_document("doc", "Doc", "all_staff", DOC)["status"] == "unchanged"
    assert before == (count("kb_parents"), count("kb_children"), count("kb_children_fts"))
    index_policy_document("doc", "Doc", "all_staff", DOC + "\n\n# Gamma\nGamma text about flanges.")
    assert count("kb_parents") == 3 and count("knowledge_documents") == 1
    assert search("flanges", STAFF) and not search("nonexistentword", STAFF)


def test_sensitive_numbers_are_redacted_before_indexing():
    index_policy_document("doc", "Doc", "all_staff", "# Notes\nCall about SSN 123-45-6789 and account 123456789012.")
    with sqlite3.connect(settings.database_path) as db:
        stored = " ".join(r[0] for r in db.execute("SELECT text FROM kb_children"))
        fts = " ".join(r[0] for r in db.execute("SELECT search_text FROM kb_children_fts"))
    assert "123-45-6789" not in stored + fts and "123456789012" not in stored + fts
    assert not search("123456789012", STAFF)


def test_invalid_input_rejected():
    for args in [("doc", "T", "everyone", DOC), ("doc", "", "all_staff", DOC), ("doc", "T", "all_staff", "   "),
                 ("bad id!", "T", "all_staff", DOC)]:
        with pytest.raises(ValueError):
            index_policy_document(*args)


def test_delete_removes_every_trace():
    index_policy_document("doc", "Doc", "all_staff", DOC)
    assert delete_policy_document("doc") is True and delete_policy_document("doc") is False
    assert [count(t) for t in ("knowledge_documents", "kb_parents", "kb_children", "kb_children_fts")] == [0, 0, 0, 0]
    assert not search("widgets", STAFF)


# ------------------------------------------------------------------ search
def test_small_to_big_returns_parent_context_with_citation():
    index_policy_document("doc", "Doc", "all_staff", DOC)
    hit = search("gadgets", STAFF)[0]
    assert hit["citation"]["parent_id"] == "doc:p1" and hit["citation"]["section"] == "Alpha"
    assert "gadgets" in hit["matched_excerpt_untrusted"]
    assert "widgets" in hit["context_untrusted"]            # the BIG parent includes text the small match lacked


def test_search_hides_restricted_documents():
    load_samples()
    assert not [r for r in search("report suspected fraud escalate", STAFF)
                if r["citation"]["document_id"] == "compliance-escalations"]
    assert search("report suspected fraud escalate", ALL)[0]["citation"]["document_id"] == "compliance-escalations"


def test_hostile_queries_are_harmless():
    index_policy_document("doc", "Doc", "all_staff", DOC)
    for q in ['" OR 1=1 --', "widgets AND NOT (", "*", "NEAR(", "widgets\"; DROP TABLE kb_parents;--", "the of a"]:
        search(q, STAFF)                                     # must not raise
    assert count("kb_parents") == 2
    assert build_match("the and of") is None and build_match('xx" OR "yy') == '"xx" OR "yy"'


def test_top_k_is_bounded_and_parents_are_unique():
    load_samples()
    results = search("client email address documents change", ALL, top_k=99)
    assert len(results) <= 5 and len({r["citation"]["parent_id"] for r in results}) == len(results)


# ------------------------------------------------------------------ AI tool + agent
def _ctx(audiences=STAFF):
    from org_automation.agent.context import RunContext
    from org_automation.ai.extraction import DemoExtractor
    from org_automation.integrations.crm import MockCrmAdapter
    from org_automation.integrations.docusign import MockDocuSignAdapter
    return RunContext(actor="flow", allowed_documents=frozenset({"env-1001"}), crm=MockCrmAdapter(),
                      docusign=MockDocuSignAdapter(), extractor=DemoExtractor(), allowed_audiences=audiences)


def test_tool_respects_audience_and_logs_what_the_ai_saw():
    load_samples()
    out = gateway.call(_ctx(), "search_policy_documents", {"query": "report suspected fraud escalate"})
    assert out["ok"] and not [r for r in out["result"]["results"] if "compliance" in r["citation"]["document_id"]]
    out = gateway.call(_ctx(ALL), "search_policy_documents", {"query": "report suspected fraud escalate"})
    assert out["result"]["results"][0]["citation"]["document_id"] == "compliance-escalations"
    with sqlite3.connect(settings.database_path) as db:
        logged = db.execute("SELECT details_json FROM audit_events WHERE event_type='kb_search'").fetchall()
    assert len(logged) == 2 and "compliance-escalations:p1" in logged[1][0]


def test_tool_rejects_bad_arguments():
    for args in [{"query": "x"}, {"query": "ok query", "top_k": 50}, {"query": "ok query", "audience": "compliance"}]:
        assert gateway.call(_ctx(), "search_policy_documents", args)["ok"] is False   # AI can't pick its own audience


def test_scripted_agent_cites_policy_in_reviewer_note():
    load_samples()
    run_agent("env-1001", "run-kb", "flow")
    note = list_pending()[0]["proposal"]["agent_note_untrusted"]
    assert "Client Data Changes Procedure" in note and "email address" in note


def test_agent_still_works_with_an_empty_library():
    run_agent("env-1001", "run-kb2", "flow")
    assert list_pending()[0]["proposal"]["agent_note_untrusted"] == "Submitted by the scripted agent."


# ------------------------------------------------------------------ API
client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}


def test_knowledge_api_roles_and_flow():
    body = {"title": "Secret", "audience": "compliance", "text": "# Hidden\nWhistleblower hotline details."}
    assert client.post("/knowledge/documents/secret", json=body, headers=SVC).status_code == 403   # robots can't edit the library
    assert client.post("/knowledge/documents/secret", json=body, headers=ALICE).json()["status"] == "indexed"
    assert client.post("/knowledge/documents/bad id", json=body, headers=ALICE).status_code == 422
    assert client.get("/knowledge/search", params={"q": "whistleblower hotline"}, headers=SVC).json()["results"] == []
    assert client.get("/knowledge/search", params={"q": "whistleblower hotline"}, headers=ALICE).json()["results"]
    assert client.get("/knowledge/documents", headers=SVC).json()["items"] == []
    assert client.get("/knowledge/search", params={"q": "ab"}, headers=ALICE).status_code == 422
    assert client.delete("/knowledge/documents/secret", headers=ALICE).json()["status"] == "deleted"
    assert client.delete("/knowledge/documents/secret", headers=ALICE).status_code == 404


def test_rag_answer_uses_retrieved_sources_and_returns_citations(monkeypatch):
    load_samples()
    from org_automation.ai.client import LlmClient
    from org_automation.core.settings import settings
    settings.llm_provider = "openai-compatible"  # constructor does not require the optional Anthropic package
    calls = {}

    def fake_answer(self, *, question, sources, max_tokens=1200):
        calls["question"] = question
        calls["sources"] = sources
        return "Keep evidence of the approval and record the decision [1]."

    monkeypatch.setattr(LlmClient, "answer_question", fake_answer)
    response = client.get("/knowledge/answer", params={"q": "approval record decision"}, headers=ALICE)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model_used"] is True
    assert "[1]" in payload["answer"]
    assert calls["question"] == "approval record decision"
    assert calls["sources"] and calls["sources"][0]["citation"]["document_id"]


def test_knowledge_answer_no_results_does_not_call_model(monkeypatch):
    from org_automation.ai.client import LlmClient
    monkeypatch.setattr(LlmClient, "answer_question", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call model")))
    response = client.get("/knowledge/answer", params={"q": "no matching policy exists"}, headers=ALICE)
    assert response.status_code == 200
    assert response.json()["model_used"] is False
    assert response.json()["results"] == []


def test_policy_pdf_upload_is_reviewer_only_and_indexes_extracted_text(monkeypatch):
    from org_automation.api import routes
    monkeypatch.setattr(routes, "pdf_to_pages", lambda data: ["# Transfer Policy\nA reviewer must approve wire transfers."])
    data = {"document_id": "uploaded-transfer", "title": "Transfer Policy", "audience": "all_staff"}
    files = {"file": ("transfer-policy.pdf", b"%PDF-fake", "application/pdf")}
    assert client.post("/knowledge/documents/upload", data=data, files=files, headers=SVC).status_code == 403
    response = client.post("/knowledge/documents/upload", data=data, files=files, headers=ALICE)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "indexed"
    assert search("who approves wire transfers", STAFF)


def test_policy_upload_rejects_non_pdf():
    data = {"document_id": "uploaded-text", "title": "Text file", "audience": "all_staff"}
    files = {"file": ("notes.txt", b"not a pdf", "text/plain")}
    response = client.post("/knowledge/documents/upload", data=data, files=files, headers=ALICE)
    assert response.status_code == 415


def test_llm_rag_adapter_sends_untrusted_passages_and_returns_text(monkeypatch):
    from types import SimpleNamespace

    from org_automation.ai.client import LlmClient
    from org_automation.core.settings import settings
    settings.llm_provider = "openai-compatible"
    client_llm = LlmClient()
    captured = {}

    def fake_call(*, system, messages, tools=None, max_tokens=1024):
        captured["system"] = system
        captured["prompt"] = messages[0]["content"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="The policy requires review [1].")])

    monkeypatch.setattr(client_llm, "_compatible_call", fake_call)
    answer = client_llm.answer_question(question="What approval is required?", sources=[{
        "citation": {"title": "Transfer Policy", "document_id": "transfer", "section": "Approval"},
        "context_untrusted": "A reviewer must approve each transfer.",
    }])
    assert answer == "The policy requires review [1]."
    assert "ONLY the supplied source passages" in captured["system"]
    assert "<untrusted_passage>" in captured["prompt"]
    assert "A reviewer must approve each transfer." in captured["prompt"]
