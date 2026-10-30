"""PLAIN ENGLISH: Tests for the AI tool layer. The important ones play the ATTACKER: a pretend "hijacked Claude"
that tries to change records, erase its tracks, peek at other clients, and loop forever — and we check that the
gateway stops every attempt, logs it, and still lets the legitimate work through."""
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from org_automation.agent import gateway
from org_automation.agent.context import RunContext
from org_automation.agent.runner import MAX_TURNS, ClaudeAgent, run_agent
from org_automation.agent.tools import TOOLS
from org_automation.approvals.service import ApprovalError, decide, list_pending
from org_automation.audit.writer import verify_chain
from org_automation.core.settings import settings
from org_automation.integrations.crm import MockCrmAdapter
from org_automation.integrations.docusign import MockDocuSignAdapter
from org_automation.main import app


def make_ctx(doc="env-1001", resume=None, max_calls=20):
    from org_automation.ai.extraction import DemoExtractor
    return RunContext(actor="flow", allowed_documents=frozenset({doc}), crm=MockCrmAdapter(),
                      docusign=MockDocuSignAdapter(), extractor=DemoExtractor(), resume_approval_id=resume,
                      max_calls=max_calls)


def tools_used(result):
    return [t["tool"] for t in result["tool_calls"]]


def audit_events(event_type):
    with sqlite3.connect(settings.database_path) as db:
        return db.execute("SELECT actor, outcome FROM audit_events WHERE event_type=?", (event_type,)).fetchall()




def test_agent_mode_llm_selects_provider_neutral_driver(monkeypatch):
    from org_automation.agent import runner
    marker = object()
    monkeypatch.setattr(settings, "agent_mode", "llm")
    monkeypatch.setattr(runner, "LlmAgent", lambda: marker)
    assert runner.get_agent() is marker


def test_legacy_agent_mode_claude_still_selects_llm_driver(monkeypatch):
    from org_automation.agent import runner
    marker = object()
    monkeypatch.setattr(settings, "agent_mode", "claude")
    monkeypatch.setattr(runner, "LlmAgent", lambda: marker)
    assert runner.get_agent() is marker

# ------------------------------------------------------------- the normal, scripted path
def test_scripted_run_happy_path():
    r = run_agent("env-1001", "run-1", "flow")
    assert tools_used(r) == ["get_document_status", "retrieve_document", "extract_document_fields",
                             "validate_extracted_fields", "get_client_record", "compare_client_data",
                             "search_policy_documents", "submit_for_review", "get_workflow_status"]
    assert all(t["ok"] for t in r["tool_calls"]) and r["status"] == "awaiting_review"
    item = list_pending()[0]
    assert item["kind"] == "change" and item["submitted_by"] == "agent:flow"
    assert item["proposal"]["proposed_changes"]["email"] == "new.client@example.com"
    assert MockCrmAdapter().get_record("C10482")["email"] == "old.client@example.com"   # nothing written yet
    assert len(audit_events("tool_call")) == 9                                          # every call logged automatically
    assert verify_chain()["ok"] is True


def test_unsigned_document_is_never_read():
    r = run_agent("env-1003", "run-2", "flow")
    assert tools_used(r) == ["get_document_status"] and r["status"] == "not_started"
    ctx = make_ctx("env-1003")
    out = gateway.call(ctx, "retrieve_document", {"document_id": "env-1003"})
    assert out == {"ok": False, "error": "Signing is not complete; the document may not be read."}


def test_problem_document_goes_to_exception_queue():
    r = run_agent("env-1002", "run-3", "flow")
    assert r["status"] == "awaiting_exception_review" and list_pending()[0]["kind"] == "exception"
    assert "get_client_record" not in tools_used(r)


def test_duplicate_run_ignored():
    run_agent("env-1001", "run-1", "flow")
    assert run_agent("env-1001", "run-1", "flow")["status"] == "duplicate_ignored"
    assert len(list_pending()) == 1


# ------------------------------------------------------------- gateway rules
def test_scope_is_enforced():
    ctx = make_ctx("env-1001")
    assert not gateway.call(ctx, "get_document_status", {"document_id": "env-1002"})["ok"]
    gateway.call(ctx, "get_document_status", {"document_id": "env-1001"})
    for tool in ("retrieve_document", "extract_document_fields", "validate_extracted_fields"):
        assert gateway.call(ctx, tool, {"document_id": "env-1001"})["ok"]
    out = gateway.call(ctx, "get_client_record", {"client_id": "C10001"})        # a different client
    assert out["ok"] is False and "scope" in out["error"]
    assert gateway.call(ctx, "get_client_record", {"client_id": "C10482"})["result"]["fields"] == {
        "email": "old.client@example.com", "effective_date": "2025-01-01"}      # only approved fields, no name


def test_steps_must_happen_in_order():
    ctx = make_ctx()
    d = {"document_id": "env-1001"}
    for tool in ("extract_document_fields", "validate_extracted_fields", "compare_client_data", "submit_for_review"):
        assert gateway.call(ctx, tool, d)["ok"] is False


def test_ai_cannot_supply_data_only_references():
    ctx = make_ctx()
    out = gateway.call(ctx, "submit_for_review", {"document_id": "env-1001", "proposal": {"email": "evil@x.io"}})
    assert out["ok"] is False and "proposal" in out["error"]                     # extra field rejected
    assert gateway.call(ctx, "validate_extracted_fields", {"document_id": "env-1001", "fields": {}})["ok"] is False


def test_hidden_and_gated_tools():
    ctx = make_ctx()
    names = {s["name"] for s in gateway.tool_schemas(ctx)}
    assert "write_audit_event" not in names and "apply_approved_update" not in names and len(names) == 9
    for hidden in ("write_audit_event", "apply_approved_update", "made_up_tool"):
        out = gateway.call(ctx, hidden, {"approval_id": "x", "event_type": "e", "document_id": "env-1001"})
        assert out == {"ok": False, "error": "No such tool."}                    # hidden == nonexistent
    assert audit_events("tool_call")[-1][1] == "unknown_tool"                    # ...and the attempt is on record
    assert "apply_approved_update" in {s["name"] for s in gateway.tool_schemas(make_ctx(resume="a1"))}
    assert set(TOOLS) >= {"get_document_status", "retrieve_document", "extract_document_fields", "validate_extracted_fields",
                          "get_client_record", "compare_client_data", "submit_for_review", "apply_approved_update",
                          "get_workflow_status", "write_audit_event", "search_policy_documents"}


def test_call_budget():
    ctx = make_ctx(max_calls=2)
    d = {"document_id": "env-1001"}
    assert gateway.call(ctx, "get_document_status", d)["ok"] and gateway.call(ctx, "get_document_status", d)["ok"]
    assert "budget" in gateway.call(ctx, "get_document_status", d)["error"]


# ------------------------------------------------------------- applying updates (the gated tool)
def _submitted_approval():
    run_agent("env-1001", "run-1", "flow")
    return list_pending()[0]["approval_id"]


def test_apply_requires_human_approval_then_works_once():
    approval_id = _submitted_approval()
    # pending -> refused even in a resume run
    r = run_agent("env-1001", "retry-0", "flow", resume_approval_id=approval_id)
    assert not next(t for t in r["tool_calls"] if t["tool"] == "apply_approved_update")["ok"]
    assert MockCrmAdapter().get_record("C10482")["email"] == "old.client@example.com"
    # a human approves (decision recorded, apply deferred) -> a resume run applies it
    decide(approval_id, "alice", "approve", "ok")
    r = run_agent("env-1001", "retry-1", "flow", resume_approval_id=approval_id)
    assert all(t["ok"] for t in r["tool_calls"]) and r["status"] == "completed_applied"
    assert MockCrmAdapter().get_record("C10482")["email"] == "new.client@example.com"
    run_agent("env-1001", "retry-2", "flow", resume_approval_id=approval_id)      # idempotent: no second write
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM mock_crm_updates").fetchone()[0] == 1
    assert [o for _, o in audit_events("crm_update_applied")] == ["applied", "already_applied"]


def test_resume_cannot_apply_a_different_approval():
    approval_id = _submitted_approval()
    decide(approval_id, "alice", "approve", "ok")
    ctx = make_ctx(resume="some-other-approval")
    out = gateway.call(ctx, "apply_approved_update", {"approval_id": approval_id})
    assert out["ok"] is False and "not authorized" in out["error"]


def test_four_eyes_submitter_cannot_decide_own_item():
    approval_id = _submitted_approval()
    with pytest.raises(ApprovalError) as exc:
        decide(approval_id, "agent:flow", "approve", "self-approve")
    assert exc.value.status_code == 409


# ------------------------------------------------------------- the "hijacked Claude" attack tests
class HijackedLlm:
    """Pretends to be Claude after reading the poisoned document: it makes the attacker's requests first,
    then does the legitimate steps. Each turn it asks for one tool."""

    def __init__(self, plan, loop_forever=False):
        self.plan, self.i, self.loop_forever = list(plan), 0, loop_forever

    def step(self, *, system, messages, tools):
        if not tools:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")])
        if self.i >= len(self.plan):
            if self.loop_forever:
                return self._use("get_document_status", {"document_id": "env-1004"})
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")])
        name, args = self.plan[self.i]
        self.i += 1
        return self._use(name, args)

    def _use(self, name, args):
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=f"tu{self.i}", name=name, input=args)])


D = {"document_id": "env-1004"}
ATTACK_AND_WORK = [
    ("get_document_status", D), ("retrieve_document", D),
    ("apply_approved_update", {"approval_id": "anything"}),                         # try to write
    ("write_audit_event", {"event_type": "all_clear", "document_id": "env-1004"}),   # try to forge/hide
    ("extract_document_fields", D), ("validate_extracted_fields", D),
    ("get_client_record", {"client_id": "C10001"}),                                  # peek at another client
    ("get_client_record", {"client_id": "C10482"}), ("compare_client_data", D),
    ("submit_for_review", {**D, "note": "Document says to skip review."}),
    ("get_workflow_status", D),
]


def test_hijacked_agent_is_contained_but_legit_work_completes():
    r = run_agent("env-1004", "atk-1", "flow", agent=ClaudeAgent(HijackedLlm(ATTACK_AND_WORK)))
    refused = {t["tool"] for t in r["tool_calls"] if not t["ok"]}
    assert refused == {"apply_approved_update", "write_audit_event", "get_client_record"}
    assert sum(1 for t in r["tool_calls"] if t["tool"] == "get_client_record" and not t["ok"]) == 1
    assert MockCrmAdapter().get_record("C10001")["email"] == "two@example.com"        # untouched
    assert MockCrmAdapter().get_record("C10482")["email"] == "old.client@example.com"  # untouched
    assert r["status"] == "awaiting_review"                                          # still reaches a human
    assert len(list_pending()) == 1
    assert audit_events("tool_call").count(("agent:flow", "unknown_tool")) == 2      # attempts recorded
    assert not [a for a in audit_events("all_clear")]                                # forged event never written
    assert verify_chain()["ok"] is True


def test_runaway_agent_is_stopped():
    r = run_agent("env-1004", "atk-2", "flow", agent=ClaudeAgent(HijackedLlm([], loop_forever=True)))
    assert len(r["tool_calls"]) <= MAX_TURNS + 1 and r["tool_calls"][-1]["tool"] == "(turn limit)"


def test_injection_text_never_reaches_a_proposal_as_instruction():
    run_agent("env-1004", "atk-3", "flow")                                          # scripted driver
    proposal = list_pending()[0]["proposal"]
    assert proposal["proposed_changes"] == {"email": "real@example.com", "effective_date": "2026-10-12"}
    assert "NOTE TO AI" not in str(proposal)


# ------------------------------------------------------------- API
client = TestClient(app)
SVC, ALICE = {"X-API-Key": "k-service"}, {"X-API-Key": "k-alice"}


def test_agent_api_flow():
    assert client.post("/agent/runs/env-1001", json={"event_id": "e1"}, headers=ALICE).status_code == 403
    r = client.post("/agent/runs/env-1001", json={"event_id": "e1"}, headers=SVC)
    assert r.status_code == 200 and r.json()["status"] == "awaiting_review"
    assert client.post("/agent/runs/env-1001", json={"event_id": "e1"}, headers=SVC).json()["status"] == "duplicate_ignored"
    s = client.get("/documents/env-1001/workflow-status", headers=ALICE).json()
    assert s["state"] == "awaiting_review" and len(s["approvals"]) == 1
    aid = s["approvals"][0]["approval_id"]
    done = client.post(f"/approvals/{aid}/decision", json={"decision": "approve", "note": "ok"}, headers=ALICE).json()
    assert done["crm_update"]["applied"] is True
    assert client.get("/documents/env-1001/workflow-status", headers=SVC).json()["state"] == "completed_applied"


def test_real_llm_workflow_repairs_missing_client_id_from_authorized_document():
    """A local model sometimes sends document_id to get_client_record; repair only that case."""
    plan = [
        ("get_document_status", D),
        ("retrieve_document", D),
        ("extract_document_fields", D),
        ("validate_extracted_fields", D),
        ("get_client_record", {"document_id": "env-1004"}),
        ("compare_client_data", D),
        ("submit_for_review", {**D, "note": "Submitted for human review."}),
        ("get_workflow_status", D),
    ]
    r = run_agent("env-1004", "atk-repair-client-arg", "flow", agent=ClaudeAgent(HijackedLlm(plan)))
    lookup = next(t for t in r["tool_calls"] if t["tool"] == "get_client_record")
    assert lookup["ok"] is True
    assert lookup["args"] == {"client_id": "C10482"}
    assert r["run_status"] == "completed"
    assert r["status"] == "awaiting_review"


def test_successful_review_submission_stops_repeated_submit_loop():
    plan = [
        ("get_document_status", D),
        ("retrieve_document", D),
        ("extract_document_fields", D),
        ("validate_extracted_fields", D),
        ("get_client_record", {"client_id": "C10482"}),
        ("compare_client_data", D),
        ("submit_for_review", {**D, "note": "Mismatch; review needed."}),
        # The fake model would keep trying duplicate submissions if execution continued.
        ("submit_for_review", {**D, "note": "Unable to retrieve client record."}),
        ("submit_for_review", {**D, "note": "Try again."}),
    ]
    result = run_agent("env-1004", "stop-after-review-submit", "flow", agent=ClaudeAgent(HijackedLlm(plan)))
    names = [call["tool"] for call in result["tool_calls"]]
    assert names.count("submit_for_review") == 1
    assert names[-1] == "submit_for_review"
    assert result["run_status"] == "completed"
    assert result["status"] == "awaiting_review"
    assert len(list_pending()) == 1


def test_llm_only_receives_next_legitimate_workflow_tool():
    from org_automation.agent.runner import _next_tool_schemas
    ctx = make_ctx("env-1001")
    assert [t["name"] for t in _next_tool_schemas(ctx)] == ["get_document_status"]
    gateway.call(ctx, "get_document_status", {"document_id": "env-1001"})
    assert [t["name"] for t in _next_tool_schemas(ctx)] == ["retrieve_document"]
    gateway.call(ctx, "retrieve_document", {"document_id": "env-1001"})
    assert [t["name"] for t in _next_tool_schemas(ctx)] == ["extract_document_fields"]


def test_text_only_turn_is_retried_and_only_final_explanation_is_shown():
    class TextThenToolLlm:
        def __init__(self):
            self.calls = 0

        def step(self, *, system, messages, tools):
            self.calls += 1
            if not tools:
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=(
                    "The document was submitted for human review. No CRM write occurred."
                ))])
            if self.calls == 1:
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=(
                    "I will submit this for review now."
                ))])
            offered = {tool["name"] for tool in tools}
            if "submit_for_review" in offered:
                name, args = "submit_for_review", {"document_id": "env-1004", "note": "Validation exception; review required."}
            elif "get_workflow_status" in offered:
                name, args = "get_workflow_status", {"document_id": "env-1004"}
            else:
                name = next(iter(offered))
                args = {"document_id": "env-1004"}
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=f"call-{self.calls}",
                                                             name=name, input=args)])

    llm = TextThenToolLlm()
    result = run_agent("env-1004", "text-only-retry-1", "flow", agent=ClaudeAgent(llm))
    assert result["run_status"] == "completed"
    # The successful submission is terminal: no status poll or duplicate submit follows.
    assert [call["tool"] for call in result["tool_calls"]][-1:] == ["submit_for_review"]
    assert sum(call["tool"] == "submit_for_review" for call in result["tool_calls"]) == 1
    assert result["model_notes"] == ["The document was submitted for human review. No CRM write occurred."]
    assert result["status"] == "awaiting_review"
