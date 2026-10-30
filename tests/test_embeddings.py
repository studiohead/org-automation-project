"""Offline tests for the optional semantic retrieval path."""

from org_automation.core.settings import settings
from org_automation.knowledge import search as search_module
from org_automation.workflows import index_policy as index_module


def test_semantic_index_and_search_use_vectors_and_keep_parent_context(monkeypatch):
    monkeypatch.setattr(settings, "embedding_mode", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "test-embed")
    # Deterministic fake vectors: query "beneficiary" is closest to the matching chunk.
    def fake_embed(texts):
        return [[1.0, 0.0] if ("beneficiaries" in t.lower() or t.strip().lower() == "beneficiary") else [0.0, 1.0] for t in texts]
    monkeypatch.setattr(index_module, "embed_texts", fake_embed)
    monkeypatch.setattr(search_module, "embed_texts", fake_embed)
    index_module.index_policy_document(
        "semantic-test", "Policy", "all_staff",
        "# Transfers\nTransfers require review.\n\n# Beneficiaries\nBeneficiary changes need verification."
    )
    result = search_module.search("beneficiary", frozenset({"all_staff"}), 2)
    assert result
    assert result[0]["retrieval"]["method"] == "semantic"
    assert result[0]["retrieval"]["cosine_similarity"] is not None
    assert result[0]["citation"]["section"] == "Beneficiaries"
    assert "verification" in result[0]["context_untrusted"].lower()
