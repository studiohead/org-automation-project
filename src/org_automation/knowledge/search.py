"""Role-filtered parent/child retrieval using local semantic vectors when indexed, else SQLite BM25."""
import json
import re

from org_automation.core.settings import settings
from org_automation.knowledge.embeddings import cosine, embed_texts
from org_automation.storage.database import connect

_STOP = frozenset(["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "how", "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "our", "should", "so", "that", "the", "their", "there", "this", "to", "we", "what", "when", "where", "which", "who", "why", "will", "with", "you", "your"])
MAX_EXCERPT = 300


def build_match(query: str) -> str | None:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1 and w not in _STOP]
    unique = list(dict.fromkeys(words))[:12]
    return " OR ".join(f'"{w}"' for w in unique) or None


def search(query: str, audiences: frozenset[str], top_k: int = 3) -> list[dict]:
    match = build_match(query)
    if not query.strip() or not audiences:
        return []
    top_k = max(1, min(top_k, 5))
    marks = ",".join("?" * len(audiences))
    with connect() as db:
        semantic_rows = []
        if settings.embedding_mode == "ollama":
            try:
                query_vector = embed_texts([query])[0]
            except RuntimeError:
                # Keep the library usable during an embedding-service outage; result labels expose BM25.
                query_vector = None
            rows = db.execute(
                f"SELECT c.child_id,c.parent_id,c.document_id,c.text,e.vector_json,e.model "
                f"FROM kb_children c JOIN kb_embeddings e ON e.child_id=c.child_id "
                f"JOIN knowledge_documents d ON d.document_id=c.document_id "
                f"WHERE d.audience IN ({marks}) AND e.model=?",
                (*sorted(audiences), settings.embedding_model)).fetchall()
            for row in rows if query_vector is not None else []:
                try:
                    score = cosine(query_vector, json.loads(row["vector_json"]))
                except (ValueError, TypeError):
                    continue
                semantic_rows.append((score, row))
            semantic_rows.sort(key=lambda x: x[0], reverse=True)

        # Keyword BM25 remains the offline fallback and can fill gaps if semantic vectors aren't available.
        keyword_rows = []
        if match:
            keyword_rows = db.execute(
                f"SELECT f.child_id, c.parent_id, bm25(kb_children_fts) AS score FROM kb_children_fts f "
                f"JOIN kb_children c ON c.child_id = f.child_id "
                f"JOIN knowledge_documents d ON d.document_id = c.document_id "
                f"WHERE kb_children_fts MATCH ? AND d.audience IN ({marks}) ORDER BY score LIMIT ?",
                (match, *sorted(audiences), top_k * 4)).fetchall()

        ranked = []
        if semantic_rows:
            ranked.extend((row["child_id"], row["parent_id"], score, "semantic") for score, row in semantic_rows[:top_k * 4])
            semantic_ids = {x[0] for x in ranked}
            ranked.extend((r["child_id"], r["parent_id"], None, "keyword") for r in keyword_rows if r["child_id"] not in semantic_ids)
        else:
            ranked.extend((r["child_id"], r["parent_id"], None, "keyword") for r in keyword_rows)

        results, seen = [], set()
        for child_id, parent_id, score, method in ranked:
            if parent_id in seen:
                continue
            parent = db.execute("SELECT p.*, d.title FROM kb_parents p JOIN knowledge_documents d "
                                "ON d.document_id = p.document_id WHERE p.parent_id=?", (parent_id,)).fetchone()
            child = db.execute("SELECT text FROM kb_children WHERE child_id=?", (child_id,)).fetchone()
            if not parent or not child:
                continue
            seen.add(parent_id)
            results.append({
                "rank": len(results) + 1,
                "retrieval": {"method": method, "cosine_similarity": round(score, 4) if score is not None else None},
                "citation": {"document_id": parent["document_id"], "title": parent["title"],
                             "section": parent["section_title"], "parent_id": parent["parent_id"],
                             "matched_child_id": child_id},
                "matched_excerpt_untrusted": child["text"][:MAX_EXCERPT],
                "context_untrusted": parent["text"],
            })
            if len(results) == top_k:
                break
    return results
