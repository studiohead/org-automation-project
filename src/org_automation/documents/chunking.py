"""PLAIN ENGLISH: The document slicer for the policy library.

Long policies are cut into two layers of pieces:
  * PARENTS = whole sections (e.g. "Changing a client's email address") — big enough to make sense on their own.
  * CHILDREN = short paragraph groups inside a section — small enough to match a question precisely.
Search looks at the small pieces (precise), then hands back the BIG piece they belong to (full context). That is
"small-to-big" retrieval. Each child remembers its parent.

Details that matter:
  * IDs are built from the document ID and position (e.g. `client-data-changes:p2:c1`), NOT random, so
    re-indexing the same document replaces the old pieces instead of duplicating them.
  * Parents have a size cap (an oversized section is split at paragraph breaks); a giant single paragraph is
    split at sentence boundaries. Nothing is silently dropped.
  * Sections are found from markdown-style headings (# / ## / ###). Detecting headings inside scanned or messy PDFs
    is a harder, separate problem that this scaffold deliberately does not attempt.
"""
import re
from dataclasses import dataclass

MAX_PARENT_CHARS = 1500    # roughly 375 tokens; kept small so results fit the tool-result size cap
MAX_CHILD_CHARS = 600      # roughly 150 tokens
_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$")


@dataclass(frozen=True)
class ParentChunk:
    parent_id: str
    document_id: str
    ordinal: int
    section_title: str
    text: str


@dataclass(frozen=True)
class ChildChunk:
    child_id: str
    parent_id: str
    document_id: str
    ordinal: int
    text: str


def validate_document_id(document_id: str) -> str:
    if not _ID.fullmatch(document_id):
        raise ValueError("document_id may contain only letters, digits, '.', '_' and '-' (max 100).")
    return document_id


def parse_sections(text: str) -> list[tuple[str, str]]:
    """Split on markdown headings -> [(section title, body)]. Text with no headings becomes one section."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            sections.append((m.group(1), []))
        else:
            if not sections:
                sections.append(("Introduction", []))
            sections[-1][1].append(line)
    out = [(title, "\n".join(body).strip()) for title, body in sections]
    return [(t, b) for t, b in out if b]


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Break an over-long paragraph at sentence ends, then (last resort) at the character limit."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        while len(sentence) > limit:                        # a single enormous sentence
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _group(paragraphs: list[str], limit: int) -> list[str]:
    """Pack paragraphs into blocks no longer than `limit`, splitting any paragraph that alone is too long."""
    units = [u for p in paragraphs for u in (_split_long(p, limit) if len(p) > limit else [p])]
    blocks, current = [], ""
    for u in units:
        if current and len(current) + 2 + len(u) > limit:
            blocks.append(current)
            current = u
        else:
            current = f"{current}\n\n{u}" if current else u
    if current:
        blocks.append(current)
    return blocks


def chunk_document(document_id: str, text: str) -> tuple[list[ParentChunk], list[ChildChunk]]:
    validate_document_id(document_id)
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    for title, body in parse_sections(text):
        parts = _group(_paragraphs(body), MAX_PARENT_CHARS)
        for part_no, part in enumerate(parts, start=1):
            ordinal = len(parents) + 1
            parent_id = f"{document_id}:p{ordinal}"
            label = title if len(parts) == 1 else f"{title} (part {part_no})"
            parents.append(ParentChunk(parent_id, document_id, ordinal, label, part))
            for c_no, child in enumerate(_group(_paragraphs(part), MAX_CHILD_CHARS), start=1):
                children.append(ChildChunk(f"{parent_id}:c{c_no}", parent_id, document_id, c_no, child))
    return parents, children
