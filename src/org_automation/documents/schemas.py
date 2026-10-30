"""PLAIN ENGLISH: The "forms" that data must fit into.

These define what an extracted document looks like: for each fact we keep the value, WHICH PAGE
it came from, the exact quoted sentence it came from (so a human — and our own checker — can
verify it), and how confident the reader was. Pydantic rejects anything that doesn't fit the form.
"""
from pydantic import BaseModel, Field


class ExtractedField(BaseModel):
    value: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_excerpt: str | None = None          # the exact words in the document that justify the value
    confidence: float | None = Field(default=None, ge=0, le=1)


class ExtractedDocument(BaseModel):
    document_id: str
    document_type: str
    client_id: ExtractedField
    effective_date: ExtractedField
    email: ExtractedField
    missing_fields: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    field: str
    code: str
    message: str


class WorkflowResult(BaseModel):
    document_id: str
    # needs_review | needs_exception_review | duplicate_ignored
    status: str
    extracted: ExtractedDocument | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)
    approval_id: str | None = None
    proposed_action: str = "none"
    audit_event_id: str
    message: str


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=2000)
