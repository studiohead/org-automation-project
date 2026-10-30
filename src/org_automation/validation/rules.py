"""PLAIN ENGLISH: The fact-checker that never trusts the reader.

After the AI (or demo reader) pulls out values, this file checks them with plain, predictable
rules — no AI involved, so it can't be talked out of its job. It checks:
  1. Required fields are present.
  2. Dates and emails are well-formed (and the date is plausible, not year 1900 or 2090).
  3. GROUNDING: every value must appear inside the quoted excerpt, and that excerpt must really
     exist in the original document. This catches AI "hallucinations" (made-up values).
  4. Confidence: if the reader wasn't sure enough, a human must look.
Any problem becomes a ValidationIssue; documents with problems never go down the "clean" path.
"""
import re
from datetime import date, timedelta

from org_automation.core.settings import settings
from org_automation.documents.schemas import ExtractedDocument, ValidationIssue

_FIELDS = ("client_id", "effective_date", "email")
SUPPORTED_DOCUMENT_TYPES = frozenset({"client_update"})   # the only form this workflow knows how to check and apply


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def validate(doc: ExtractedDocument, pages: list[str] | None = None) -> list[ValidationIssue]:
    # Work out "missing" from the values themselves. The reader's own missing_fields list is a claim, not a fact: a
    # reader (or a model) that forgets to list a blank field must not be able to slip an empty value through.
    issues = [
        ValidationIssue(field=f, code="missing_field", message="Required field was not extracted.")
        for f in _FIELDS if not getattr(doc, f).value
    ]

    if doc.effective_date.value:
        try:
            d = date.fromisoformat(doc.effective_date.value)
            if d < date(2000, 1, 1) or d > date.today() + timedelta(days=730):
                issues.append(ValidationIssue(field="effective_date", code="implausible_date",
                                              message="Date is outside the plausible range."))
        except ValueError:
            issues.append(ValidationIssue(field="effective_date", code="invalid_date", message="Expected YYYY-MM-DD."))

    if doc.email.value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", doc.email.value):
        issues.append(ValidationIssue(field="email", code="invalid_email", message="Email format is invalid."))

    if doc.document_type == "unknown":
        issues.append(ValidationIssue(field="document_type", code="unknown_type", message="Identify document type first."))
    elif doc.document_type not in SUPPORTED_DOCUMENT_TYPES:
        issues.append(ValidationIssue(field="document_type", code="unsupported_type",
                                      message="This workflow only handles client_update forms; a person must look at this one."))

    for name in _FIELDS:
        f = getattr(doc, name)
        if not f.value:
            continue
        # Grounding: the quoted excerpt must contain the value AND appear in the real document.
        if not f.source_excerpt:
            issues.append(ValidationIssue(field=name, code="missing_excerpt", message="No supporting excerpt was given."))
        elif _norm(f.value) not in _norm(f.source_excerpt):
            issues.append(ValidationIssue(field=name, code="ungrounded_value", message="Value is not in its own excerpt."))
        elif pages is not None:
            if f.source_page and f.source_page > len(pages):   # a page that does not exist is a wrong claim, not "search everywhere"
                issues.append(ValidationIssue(field=name, code="ungrounded_value",
                                              message=f"Cited page {f.source_page} does not exist (document has {len(pages)})."))
            else:
                where = [pages[f.source_page - 1]] if f.source_page else pages
                if not any(_norm(f.source_excerpt) in _norm(p) for p in where):
                    issues.append(ValidationIssue(field=name, code="ungrounded_value",
                                                  message="Excerpt was not found in the document (possible hallucination)."))
        if f.confidence is None or f.confidence < settings.min_confidence:
            shown = "missing" if f.confidence is None else f"{f.confidence:.2f}"
            issues.append(ValidationIssue(field=name, code="low_confidence",
                                          message=f"Confidence {shown} is below {settings.min_confidence:.2f}."))
    return issues
