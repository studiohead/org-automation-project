"""PLAIN ENGLISH: The report card for the policy search.

Asks the library 10 practice questions and grades it:
  * hit@1   – was the right policy section the FIRST result?
  * hit@3   – was it somewhere in the top 3?
  * LEAKS   – did a restricted document show up for someone not cleared to see it? Goal: always 0.
Run it BEFORE deciding whether keyword search is good enough. If paraphrase questions fail a lot, that's the
evidence for adding embeddings (and for the vendor/governance review that comes with them). Uses a throw-away DB.
  python scripts/evaluate_retrieval.py            # report
  python scripts/evaluate_retrieval.py --strict   # exit code 1 if any leak (for CI)
"""
import argparse
import json
import tempfile
from pathlib import Path

from org_automation.core.settings import settings

ROOT = Path(__file__).resolve().parent.parent
AUDIENCE_SETS = {"all_staff": frozenset({"all_staff"}), "advisors": frozenset({"all_staff", "advisors"}),
                 "compliance": frozenset({"all_staff", "advisors", "compliance"})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    settings.database_path = str(Path(tempfile.mkdtemp()) / "kb_eval.sqlite3")

    from org_automation.knowledge.loader import load_policy_dir
    from org_automation.knowledge.search import search
    from org_automation.workflows.index_policy import index_policy_document

    for d in load_policy_dir(ROOT / "data" / "policies"):
        index_policy_document(d["document_id"], d["title"], d["audience"], d["text"], actor="eval")
    cases = [json.loads(line) for line in (ROOT / "data" / "retrieval_eval.jsonl").read_text().splitlines() if line.strip()]

    scored = hit1 = hit3 = leaks = 0
    print(f"{'case':<28}{'hit@1':<8}{'hit@3':<8}top result")
    for c in cases:
        results = search(c["query"], AUDIENCE_SETS[c["audience"]], top_k=3)
        docs = [r["citation"]["document_id"] for r in results]
        leaked = c.get("forbid_doc") in docs
        leaks += leaked
        top = f"{docs[0]} / {results[0]['citation']['section']}" if results else "(nothing)"
        if c["expect_doc"] is None:
            print(f"{c['id']:<28}{'-':<8}{'-':<8}{top}   {'LEAK!' if leaked else 'ok: restricted doc hidden'}")
            continue
        def ok(r, c=c):
            return r["citation"]["document_id"] == c["expect_doc"] and c["expect_section"] in r["citation"]["section"].lower()
        h1, h3 = bool(results) and ok(results[0]), any(ok(r) for r in results)
        scored += 1
        hit1 += h1
        hit3 += h3
        print(f"{c['id']:<28}{'yes' if h1 else 'NO':<8}{'yes' if h3 else 'NO':<8}{top}")
    print(f"\nhit@1: {hit1}/{scored}   hit@3: {hit3}/{scored}   LEAKS: {leaks}   <- restricted text shown to the wrong audience; goal 0")
    return 1 if (args.strict and leaks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
