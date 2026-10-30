"""PLAIN ENGLISH: Tests that the reader and the fact-checker behave — including that the checker
catches an AI that makes things up, and that sensitive numbers are blacked out before reaching the AI."""
from org_automation.ai.extraction import ClaudeExtractor, DemoExtractor
from org_automation.validation.rules import validate

GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: client@example.com"


def codes(issues):
    return {i.code for i in issues}


def test_demo_extracts_and_validates():
    doc = DemoExtractor().extract("d1", "client_update", GOOD)
    assert doc.client_id.value == "C10482" and doc.client_id.source_page == 1
    assert validate(doc, [GOOD]) == []


def test_missing_fields_are_reported():
    doc = DemoExtractor().extract("d2", "client_update", "Client ID: C1")
    assert "missing_field" in codes(validate(doc, ["Client ID: C1"]))


def test_page_numbers_are_tracked():
    pages = ["Client ID: C10001", "Effective date: 2026-09-01\nEmail: a@example.com"]
    doc = DemoExtractor().extract("d3", "client_update", pages)
    assert (doc.client_id.source_page, doc.email.source_page) == (1, 2)


def test_bad_formats_and_dates_are_flagged():
    text = "Client ID: C1\nEffective date: 1999-01-01\nEmail: nope"
    found = codes(validate(DemoExtractor().extract("d4", "client_update", text), [text]))
    assert {"implausible_date", "invalid_email"} <= found


class FakeLlm:
    def __init__(self, answer):
        self.answer, self.seen_pages = answer, None

    def extract_structured(self, *, pages, field_names):
        self.seen_pages = pages
        return self.answer


def _answer(excerpt_email="Email: client@example.com", conf=0.97):
    f = lambda v, e: {"value": v, "page": 1, "excerpt": e, "confidence": conf}
    return {"client_id": f("C10482", "Client ID: C10482"), "effective_date": f("2026-10-12", "Effective date: 2026-10-12"),
            "email": f("client@example.com", excerpt_email)}


def test_claude_extractor_good_answer_passes():
    doc = ClaudeExtractor(FakeLlm(_answer())).extract("d5", "client_update", GOOD)
    assert validate(doc, [GOOD]) == []


def test_hallucinated_excerpt_is_caught():
    # The "AI" claims the document says something it never said.
    fake = _answer(excerpt_email="Email: client@example.com (verified by compliance)")
    doc = ClaudeExtractor(FakeLlm(fake)).extract("d6", "client_update", GOOD)
    assert "ungrounded_value" in codes(validate(doc, [GOOD]))


def test_value_not_in_its_excerpt_is_caught():
    fake = _answer()
    fake["email"]["value"] = "attacker@evil.example"
    doc = ClaudeExtractor(FakeLlm(fake)).extract("d7", "client_update", GOOD)
    assert "ungrounded_value" in codes(validate(doc, [GOOD]))


def test_low_confidence_goes_to_a_human():
    doc = ClaudeExtractor(FakeLlm(_answer(conf=0.4))).extract("d8", "client_update", GOOD)
    assert "low_confidence" in codes(validate(doc, [GOOD]))


def test_sensitive_numbers_are_redacted_before_reaching_the_ai():
    llm = FakeLlm(_answer())
    ClaudeExtractor(llm).extract("d9", "client_update", GOOD + "\nSSN: 123-45-6789\nAcct: 123456789012")
    sent = " ".join(llm.seen_pages)
    assert "123-45-6789" not in sent and "123456789012" not in sent and "[REDACTED-SSN]" in sent
