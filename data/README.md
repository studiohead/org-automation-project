# data/ — practice documents (all fake)

PLAIN ENGLISH: `synthetic_eval.jsonl` is an "answer key". Each line is one made-up document plus
the correct answers a human would give, and where it SHOULD end up (`review` = looks clean, still
needs a human sign-off; `exception` = something is wrong, a human must look).
`scripts/evaluate_extraction.py` grades the extractor against it. Some cases are deliberate traps
(old-vs-new email, a hidden "ignore your instructions" message) so the report shows real weaknesses.
Add your own cases here as you meet real-world document quirks. **Never put real client data in this folder.**
