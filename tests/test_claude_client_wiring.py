"""PLAIN ENGLISH: Checks that our request to Claude is shaped correctly (forced form-filling, document
wrapped in tags as untrusted data) and that we read Claude's reply correctly — using a pretend
Claude, so no internet or API key is needed. (A live call still needs a real key; see README.)"""
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")

from org_automation.ai.client import LlmClient


def test_request_shape_and_response_parsing():
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        block = SimpleNamespace(type="tool_use", name="record_extraction",
                                input={"client_id": {"value": "C1", "page": 1, "excerpt": "Client ID: C1", "confidence": 0.9}})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="..."), block])

    llm = LlmClient("test-key", "claude-sonnet-5-5")
    llm._client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    out = llm.extract_structured(pages=["Client ID: C1"], field_names=["client_id"])

    assert out["client_id"]["value"] == "C1"
    assert seen["tool_choice"] == {"type": "tool", "name": "record_extraction"}   # must answer via the form
    assert '<document page="1">' in seen["messages"][0]["content"]                 # text wrapped as data
    assert "untrusted" in seen["system"].lower()
    assert seen["model"] == "claude-sonnet-5-5"
