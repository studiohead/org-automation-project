"""PLAIN ENGLISH: The "black marker" for sensitive numbers.

Before text leaves our walls (sent to an AI) or goes into a log, this blanks out things that look
like Social Security numbers or long account numbers. It is a safety net, not a guarantee —
real deployments should add a proper data-loss-prevention policy on top.
"""
import re
from typing import Any

# Lookarounds, not \b: a word boundary does not exist between a letter and a digit, so "acct123456789" or "SSN123-45-6789"
# would slip straight through. These only require that the run is not part of a longer run of digits.
_SSN = r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"
_LONG_NUMBER = r"(?<!\d)\d{9,}(?!\d)"  # account / routing / card-like digit runs, of any length from 9 up
# Identifiers are NOT secrets and must survive intact, or logs stop matching the records they describe. A random UUID
# (e.g. an approval id) contains a run of 9+ digits about 3 times in 100, and a hash digest far more often. So UUIDs and
# long hex digests (at least 32 characters, containing at least one letter) are matched first and left alone.
_UUID = r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])"
_HEX_DIGEST = r"(?<![0-9A-Za-z])(?=[0-9]*[a-fA-F])[0-9a-fA-F]{32,}(?![0-9A-Za-z])"
_PATTERN = re.compile(f"(?P<keep>{_UUID}|{_HEX_DIGEST})|(?P<ssn>{_SSN})|(?P<num>{_LONG_NUMBER})")


def _mask(m: re.Match) -> str:
    if m.group("keep") is not None:
        return m.group("keep")
    return "[REDACTED-SSN]" if m.group("ssn") is not None else "[REDACTED-NUMBER]"


def redact_text(text: str) -> str:
    return _PATTERN.sub(_mask, text)


def scrub(value: Any) -> Any:
    """Apply redact_text to every string inside nested dicts/lists (used on audit-log details)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value
