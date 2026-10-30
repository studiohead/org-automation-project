"""PLAIN ENGLISH: The policy library's shelves — adding, replacing and removing documents.

  * replace_document : swaps a document's old pieces for new ones in ONE all-or-nothing step, so a crash can
                       never leave half an old version and half a new one.
  * delete_document  : removes the document and every piece of it, including its search-index entries
                       (the "right to be forgotten" / retention requirement).
  * list_documents   : which documents a caller is allowed to know exist.
Every document carries an AUDIENCE label (all_staff / advisors / compliance) used later to hide restricted
policies from people and robots who shouldn't see them.
"""
from datetime import UTC, datetime

from org_automation.documents.chunking import ChildChunk, ParentChunk
from org_automation.storage.database import connect

AUDIENCES = ("all_staff", "advisors", "compliance")


def has_embeddings(document_id: str, model: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT COUNT(*) AS n FROM kb_embeddings e JOIN kb_children c ON c.child_id=e.child_id "
                         "WHERE c.document_id=? AND e.model=?", (document_id, model)).fetchone()
        total = db.execute("SELECT COUNT(*) AS n FROM kb_children WHERE document_id=?", (document_id,)).fetchone()
    return bool(total["n"]) and row["n"] == total["n"]


def get_hash(document_id: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT content_hash FROM knowledge_documents WHERE document_id=?", (document_id,)).fetchone()
    return row["content_hash"] if row else None


def _wipe(db, document_id: str) -> None:
    db.execute("DELETE FROM kb_children_fts WHERE document_id=?", (document_id,))
    db.execute("DELETE FROM kb_embeddings WHERE child_id IN (SELECT child_id FROM kb_children WHERE document_id=?)", (document_id,))
    db.execute("DELETE FROM kb_children WHERE document_id=?", (document_id,))
    db.execute("DELETE FROM kb_parents WHERE document_id=?", (document_id,))
    db.execute("DELETE FROM knowledge_documents WHERE document_id=?", (document_id,))


def replace_document(document_id: str, title: str, audience: str, content_hash: str,
                     parents: list[ParentChunk], children: list[ChildChunk], embeddings: dict[str, list[float]] | None = None) -> None:
    titles = {p.parent_id: p.section_title for p in parents}
    with connect() as db:                      # one transaction: all of it happens, or none of it
        _wipe(db, document_id)
        db.execute("INSERT INTO knowledge_documents VALUES (?, ?, ?, ?, ?)",
                   (document_id, title, audience, content_hash, datetime.now(UTC).isoformat()))
        db.executemany("INSERT INTO kb_parents VALUES (?, ?, ?, ?, ?)",
                       [(p.parent_id, p.document_id, p.ordinal, p.section_title, p.text) for p in parents])
        db.executemany("INSERT INTO kb_children VALUES (?, ?, ?, ?, ?)",
                       [(c.child_id, c.parent_id, c.document_id, c.ordinal, c.text) for c in children])
        db.executemany("INSERT INTO kb_children_fts(search_text, child_id, document_id) VALUES (?, ?, ?)",
                       [(f"{title} {titles[c.parent_id]} {c.text}", c.child_id, c.document_id) for c in children])
        if embeddings:
            import json

            from org_automation.core.settings import settings
            db.executemany("INSERT INTO kb_embeddings(child_id, model, vector_json) VALUES (?, ?, ?)",
                           [(child_id, settings.embedding_model, json.dumps(vector, separators=(",", ":")))
                            for child_id, vector in embeddings.items()])


def delete_document(document_id: str) -> bool:
    with connect() as db:
        existed = db.execute("SELECT 1 FROM knowledge_documents WHERE document_id=?", (document_id,)).fetchone()
        _wipe(db, document_id)
    return existed is not None


def list_documents(audiences: frozenset[str]) -> list[dict]:
    marks = ",".join("?" * len(audiences))
    with connect() as db:
        rows = db.execute(f"SELECT document_id, title, audience, indexed_at FROM knowledge_documents "
                          f"WHERE audience IN ({marks}) ORDER BY document_id", tuple(audiences)).fetchall()
    return [dict(r) for r in rows]
