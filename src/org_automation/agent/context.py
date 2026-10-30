"""PLAIN ENGLISH: The "case file" for ONE agent run.

It records who started the run, WHICH document(s) the agent is allowed to touch (its "scope"), how many tool
calls it has used, and — most importantly — the facts gathered so far (the document text, what was extracted,
what the CRM said). That data lives HERE, on the server. The AI only ever gets summaries and hands back short
references like a document ID. It can never type in "the values to validate", so a hijacked AI can't swap in
doctored data between steps.
"""
import uuid
from dataclasses import dataclass, field

from org_automation.core.settings import settings
from org_automation.documents.schemas import ExtractedDocument, ValidationIssue


@dataclass
class DocState:
    signing_status: str | None = None
    pages: list[str] | None = None
    extracted: ExtractedDocument | None = None
    issues: list[ValidationIssue] = field(default_factory=list)
    validated: bool = False
    current: dict | None = None          # CRM fields (allow-listed only) for this document's client
    compared: bool = False
    client_record_read: bool = False
    approval_id: str | None = None


@dataclass
class RunContext:
    actor: str                                   # the authenticated caller that started the run
    allowed_documents: frozenset[str]            # the ONLY document ids the agent may touch
    crm: object
    docusign: object
    extractor: object
    resume_approval_id: str | None = None        # set only for "retry an already-approved update" runs
    allowed_audiences: frozenset[str] = frozenset({"all_staff"})   # which policy documents the agent may see
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    max_calls: int = field(default_factory=lambda: settings.agent_max_tool_calls)
    calls: int = 0
    docs: dict[str, DocState] = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)   # every request: step, tool, arguments, gateway decision
    driver: str = "scripted"                     # "scripted" (fixed checklist) or "llm" (configured model chooses)
    notes: list[str] = field(default_factory=list)   # anything the model said in plain text between tool requests
    run_status: str = "incomplete"                 # current run outcome; separate from historical document workflow state

    def state(self, document_id: str) -> DocState:
        return self.docs.setdefault(document_id, DocState())
