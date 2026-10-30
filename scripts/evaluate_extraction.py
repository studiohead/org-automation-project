"""PLAIN ENGLISH: The report card for the document reader.

Runs every practice document in data/synthetic_eval.jsonl through the reader and the fact-checker,
then grades it. The numbers that matter most to a compliance-minded firm:
  * Field accuracy         – how often each extracted value is right.
  * Routing accuracy       – did the document land in the right queue (clean vs. needs-attention)?
  * BAD PASSES (the scary one) – a WRONG value that the checks still labelled "clean". Goal: zero.
Usage:
  python scripts/evaluate_extraction.py                 # demo (pattern-matching) baseline
  python scripts/evaluate_extraction.py --mode claude   # real Claude (needs ANTHROPIC_API_KEY)
  python scripts/evaluate_extraction.py --strict        # exit code 1 if any bad pass (for CI gates)
Uses a throw-away database, so it never touches your dev data.
"""
import argparse
import json
import tempfile
from pathlib import Path

from org_automation.core.settings import settings

FIELDS = ("client_id", "effective_date", "email")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["demo", "claude"], default="demo")
    ap.add_argument("--dataset", default=str(Path(__file__).resolve().parent.parent / "data" / "synthetic_eval.jsonl"))
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    settings.database_path = str(Path(tempfile.mkdtemp()) / "eval.sqlite3")  # throw-away DB
    settings.extraction_mode = args.mode

    from org_automation.ai.extraction import get_extractor
    from org_automation.integrations.crm import MockCrmAdapter
    from org_automation.workflows.process_document import run_checks

    extractor, crm = get_extractor(), MockCrmAdapter()
    cases = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]

    field_ok = {f: 0 for f in FIELDS}
    docs_perfect = route_ok = bad_passes = exceptions = 0
    print(f"Extractor: {type(extractor).__name__}   Documents: {len(cases)}\n")
    print(f"{'case':<22}{'fields ok':<11}{'routed':<11}{'expected':<11}verdict")
    for c in cases:
        ext = extractor.extract(c["id"], c["document_type"], c["pages"])
        issues, _ = run_checks(ext, c["pages"], crm)
        routed = "exception" if issues else "review"
        right = {f: getattr(ext, f).value == c["expected"][f] for f in FIELDS}
        for f, ok in right.items():
            field_ok[f] += ok
        perfect = all(right.values())
        docs_perfect += perfect
        route_ok += routed == c["expected_route"]
        exceptions += routed == "exception"
        bad = routed == "review" and not perfect
        bad_passes += bad
        verdict = "BAD PASS (wrong data looked clean)" if bad else ("ok" if routed == c["expected_route"] else "wrong queue (safe)")
        print(f"{c['id']:<22}{sum(right.values())}/{len(FIELDS):<9}{routed:<11}{c['expected_route']:<11}{verdict}")

    n = len(cases)
    print("\nField accuracy:  " + "   ".join(f"{f}={field_ok[f]}/{n}" for f in FIELDS))
    print(f"Fully correct documents: {docs_perfect}/{n}")
    print(f"Routing accuracy:        {route_ok}/{n}   (exception rate {exceptions}/{n})")
    print(f"BAD PASSES:              {bad_passes}   <- wrong data that looked clean; the goal is 0")
    return 1 if (args.strict and bad_passes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
