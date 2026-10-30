"""PLAIN ENGLISH: Reads policy files from a folder so they can be loaded into the library.

Each file is markdown with a tiny header, e.g.
    ---
    title: Client Data Changes
    audience: all_staff
    ---
    # Section heading ...
The file name (minus .md) becomes the document ID.
"""
from pathlib import Path


def parse_policy_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---\n"):
        header, _, body = raw[4:].partition("\n---\n")
        for line in header.splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return {"document_id": path.stem, "title": meta.get("title", path.stem), "audience": meta.get("audience", "all_staff"),
            "text": body}


def load_policy_dir(folder: Path) -> list[dict]:
    return [parse_policy_file(p) for p in sorted(folder.glob("*.md"))]
