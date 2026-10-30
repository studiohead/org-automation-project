"""PLAIN ENGLISH: The "add this policy to the library" process (separate from document-processing on purpose).

Steps: black-out sensitive numbers -> skip if nothing changed (same fingerprint) -> slice into parent/child pieces
-> swap into the library in one all-or-nothing step -> write to the audit diary. Re-running with the same text does
nothing; re-running with edited text cleanly replaces the old version (no duplicates).
Also handles removal (retention / "please forget this document"), which is audited too.
"""
import hashlib

from org_automation.audit.writer import write_event
from org_automation.core.redaction import redact_text
from org_automation.core.settings import settings
from org_automation.documents.chunking import chunk_document, validate_document_id
from org_automation.knowledge import store
from org_automation.knowledge.embeddings import embed_texts

MAX_TEXT = 200_000


def index_policy_document(document_id: str, title: str, audience: str, text: str, actor: str = "system") -> dict:
    validate_document_id(document_id)
    if audience not in store.AUDIENCES:
        raise ValueError(f"audience must be one of {store.AUDIENCES}")
    if not title.strip() or len(title) > 200:
        raise ValueError("title must be 1-200 characters.")
    if not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"text must be 1-{MAX_TEXT} characters.")

    clean = redact_text(text)                       # sensitive numbers never reach the searchable library
    content_hash = hashlib.sha256(f"{title}\n{audience}\n{clean}".encode()).hexdigest()
    vectors_current = settings.embedding_mode != "ollama" or store.has_embeddings(document_id, settings.embedding_model)
    if store.get_hash(document_id) == content_hash and vectors_current:
        write_event("kb_index", document_id, "unchanged", {"audience": audience}, actor)
        return {"document_id": document_id, "status": "unchanged"}

    parents, children = chunk_document(document_id, clean)
    if not children:
        raise ValueError("No indexable content found.")
    parent_titles = {p.parent_id: p.section_title for p in parents}
    embedding_error = None
    try:
        vectors = embed_texts([f"{title}\n{parent_titles[c.parent_id]}\n{c.text}" for c in children])
    except RuntimeError as exc:   # the optional embedding service is down: index for keyword search and say so
        vectors, embedding_error = [], str(exc)[:120]
    embedded = {c.child_id: v for c, v in zip(children, vectors)} if vectors else None
    store.replace_document(document_id, title, audience, content_hash, parents, children, embedded)
    write_event("kb_index", document_id, "indexed",
                {"audience": audience, "parents": len(parents), "children": len(children),
                 "content_hash": content_hash[:16], "embeddings": len(embedded or {}), "embedding_model": settings.embedding_model if embedded else None,
                 "embedding_error": embedding_error}, actor)
    result = {"document_id": document_id, "status": "indexed", "parents": len(parents), "children": len(children)}
    if embedding_error:
        result["warning"] = "Indexed for keyword search only; the embedding service was unavailable. Re-index later to add vectors."
    return result


def delete_policy_document(document_id: str, actor: str = "system") -> bool:
    validate_document_id(document_id)
    existed = store.delete_document(document_id)
    write_event("kb_delete", document_id, "deleted" if existed else "not_found", {}, actor)
    return existed
