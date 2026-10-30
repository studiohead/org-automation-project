"""Optional local embeddings via Ollama. No document text leaves the configured local endpoint.

The provider is deliberately separate from the answer-generation LLM. Set EMBEDDING_MODE=ollama
and install/pull EMBEDDING_MODEL (default nomic-embed-text) to enable vector retrieval.
"""
import math

import httpx

from org_automation.core.settings import settings


def embed_texts(texts: list[str]) -> list[list[float]]:
    if settings.embedding_mode != "ollama":
        return []
    if not texts:
        return []
    base = settings.embedding_base_url.rstrip("/")
    if not base.startswith(("http://localhost", "http://127.0.0.1", "http://host.docker.internal", "https://")):
        raise RuntimeError("Embedding endpoint must be local or HTTPS.")
    try:
        response = httpx.post(f"{base}/api/embed", json={"model": settings.embedding_model, "input": texts},
                              timeout=90, follow_redirects=False)
        response.raise_for_status()
        data = response.json()
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError("Embedding endpoint returned an unexpected response.")
        if any(not isinstance(v, list) or not v or any(not isinstance(n, (int, float)) for n in v) for v in vectors):
            raise RuntimeError("Embedding endpoint returned invalid vectors.")
        dims = len(vectors[0])
        if any(len(v) != dims for v in vectors):
            raise RuntimeError("Embedding vectors have inconsistent dimensions.")
        return vectors
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise RuntimeError(f"Embedding request failed ({type(exc).__name__}); check Ollama and the embedding model.") from exc


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or not b:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else -1.0
