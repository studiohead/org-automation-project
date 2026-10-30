"""PLAIN ENGLISH: Loads the sample policy files into the searchable library (run it once, or any time files change).
Usage:  python scripts/index_policies.py [folder]      (default: data/policies)
Safe to re-run: unchanged files are skipped, edited files replace their old version."""
import sys
from pathlib import Path

from org_automation.knowledge.loader import load_policy_dir
from org_automation.workflows.index_policy import index_policy_document


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data" / "policies"
    for doc in load_policy_dir(folder):
        result = index_policy_document(doc["document_id"], doc["title"], doc["audience"], doc["text"], actor="script")
        print(f"{doc['document_id']:<28}{doc['audience']:<12}{result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
