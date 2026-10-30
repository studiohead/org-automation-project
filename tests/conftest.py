"""PLAIN ENGLISH: Shared test set-up. Before EVERY test, give it a brand-new empty database and
fake passwords, so tests can't interfere with each other or with real data."""
import pytest
from pydantic import SecretStr

from org_automation.core.settings import settings

KEYS = {"service": "k-service", "alice": "k-alice", "bob": "k-bob"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(settings, "api_keys", "flow:k-service:service,alice:k-alice:reviewer,bob:k-bob:reviewer")
    monkeypatch.setattr(settings, "docusign_hmac_secret", SecretStr("test-secret"))
    monkeypatch.setattr(settings, "extraction_mode", "demo")
    monkeypatch.setattr(settings, "agent_mode", "scripted")
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "llm_model", "llama3.1")
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "embedding_mode", "none")
    monkeypatch.setattr(settings, "embedding_model", "nomic-embed-text")
    monkeypatch.setattr(settings, "embedding_base_url", "http://localhost:11434")
    monkeypatch.setattr(settings, "min_confidence", 0.85)
