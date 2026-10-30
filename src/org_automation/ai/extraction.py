"""PLAIN ENGLISH: The document reader — two interchangeable versions.

  * DemoExtractor   : a simple pattern-matcher ("find the line that starts with 'Email:'").
                      Free, offline, predictable. Serves as the BASELINE to beat.
  * ClaudeExtractor : asks Claude to read the document, which copes with messy real-world wording.

Both return the same shape (value + page + quoted excerpt + confidence). Neither is trusted blindly:
the validation step later double-checks every answer against the original text.
`get_extractor()` picks one based on the EXTRACTION_MODE setting.
"""
import re

from org_automation.core.redaction import redact_text
from org_automation.core.settings import settings
from org_automation.documents.schemas import ExtractedDocument, ExtractedField

FIELD_NAMES = ["client_id", "effective_date", "email"]

_PATTERNS = {
    "client_id": r"Client\s*ID\s*:\s*([A-Za-z0-9_-]+)",
    "effective_date": r"Effective\s*date\s*:\s*(\d{4}-\d{2}-\d{2})",
    "email": r"Email\s*:\s*([^\s]+)",
}


def as_pages(text_or_pages: str | list[str]) -> list[str]:
    """A plain string may mark page breaks with the form-feed character (\\f)."""
    if isinstance(text_or_pages, str):
        return text_or_pages.split("\f")
    return text_or_pages


class DemoExtractor:
    def extract(self, document_id: str, document_type: str, text_or_pages: str | list[str]) -> ExtractedDocument:
        pages = as_pages(text_or_pages)
        fields: dict[str, ExtractedField] = {}
        for name, pattern in _PATTERNS.items():
            fields[name] = ExtractedField()
            for page_no, page in enumerate(pages, start=1):
                m = re.search(pattern, page, re.IGNORECASE | re.MULTILINE)
                if m:
                    line = page[page.rfind("\n", 0, m.start()) + 1:]
                    line = line.split("\n", 1)[0].strip()
                    fields[name] = ExtractedField(value=m.group(1).strip(), source_page=page_no,
                                                  source_excerpt=line, confidence=1.0)
                    break
        missing = [n for n, f in fields.items() if not f.value]
        return ExtractedDocument(document_id=document_id, document_type=document_type,
                                 missing_fields=missing, **fields)


# Kept so older code/tests that import ExtractionService still work.
ExtractionService = DemoExtractor


class ClaudeExtractor:
    def __init__(self, llm=None):
        if llm is None:
            from org_automation.ai.client import LlmClient
            if settings.llm_provider == "claude" and not settings.anthropic_api_key:
                raise RuntimeError("Claude provider requires ANTHROPIC_API_KEY.")
            llm = LlmClient()
        self._llm = llm

    def extract(self, document_id: str, document_type: str, text_or_pages: str | list[str]) -> ExtractedDocument:
        pages = [redact_text(p) for p in as_pages(text_or_pages)]  # black-marker first
        raw = self._llm.extract_structured(pages=pages, field_names=FIELD_NAMES)
        fields: dict[str, ExtractedField] = {}
        for name in FIELD_NAMES:
            item = raw.get(name) if isinstance(raw, dict) else None
            item = item if isinstance(item, dict) else {}   # anything but an object means "not extracted"
            value = (item.get("value") or None)
            page = item.get("page")
            try:
                conf = float(item["confidence"]) if item.get("confidence") is not None else None
            except (TypeError, ValueError):
                conf = None
            fields[name] = ExtractedField(
                value=value.strip() if isinstance(value, str) else None,
                source_page=page if isinstance(page, int) and page >= 1 else None,
                source_excerpt=item.get("excerpt") if isinstance(item.get("excerpt"), str) else None,
                confidence=min(max(conf, 0.0), 1.0) if conf is not None else None,
            )
        missing = [n for n, f in fields.items() if not f.value]
        return ExtractedDocument(document_id=document_id, document_type=document_type,
                                 missing_fields=missing, **fields)


def get_extractor():
    return ClaudeExtractor() if settings.extraction_mode == "claude" else DemoExtractor()
