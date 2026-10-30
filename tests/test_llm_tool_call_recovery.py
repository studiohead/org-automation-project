
from org_automation.ai.client import LlmClient

TOOLS = [
    {
        "name": "retrieve_document",
        "description": "Retrieve the authorized document",
        "input_schema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    }
]


def test_recovers_tool_json_after_natural_language_preamble():
    text = (
        'Since the signing status is "completed", I will proceed with retrieving the document. '
        '{"name": "retrieve_document", "parameters": {"document_id":"env-1004"}}'
    )
    assert LlmClient._recover_text_tool_call(text, TOOLS) == (
        "retrieve_document", {"document_id": "env-1004"}
    )


def test_recovers_fenced_openai_function_shape_and_json_arguments():
    text = '```json\n{"function":{"name":"retrieve_document","arguments":"{\\"document_id\\":\\"env-1004\\"}"}}\n```'
    assert LlmClient._recover_text_tool_call(text, TOOLS) == (
        "retrieve_document", {"document_id": "env-1004"}
    )


def test_does_not_recover_unoffered_or_malformed_tools():
    assert LlmClient._recover_text_tool_call(
        '{"name":"apply_approved_update","parameters":{"approval_id":"x"}}', TOOLS
    ) is None
    assert LlmClient._recover_text_tool_call(
        '{"name":"retrieve_document","parameters":"not-json"}', TOOLS
    ) is None
    assert LlmClient._recover_text_tool_call('{"ordinary":"json"}', TOOLS) is None


def test_compatible_call_turns_text_json_into_structured_tool_request():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                'I will retrieve it now. {"name":"retrieve_document",'
                '"parameters":{"document_id":"env-1004"}}'
            )}}]}

    class FakeHTTP:
        def post(self, *args, **kwargs):
            return FakeResponse()

    client = LlmClient.__new__(LlmClient)
    client._http = FakeHTTP()
    client._base_url = "http://localhost:11434/v1"
    client._model = "llama3.1"
    client._api_key = "ollama"

    response = client._compatible_call(system="test", messages=[{"role": "user", "content": "go"}], tools=TOOLS)
    requests = [block for block in response.content if block.type == "tool_use"]
    assert len(requests) == 1
    assert requests[0].name == "retrieve_document"
    assert requests[0].input == {"document_id": "env-1004"}
    assert not any(block.type == "text" for block in response.content)
