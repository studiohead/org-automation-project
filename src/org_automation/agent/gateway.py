"""PLAIN ENGLISH: The security checkpoint every tool call must pass through.

The AI never runs code itself. It *asks* for a tool by name; this gateway decides. For each request it:
  1. refuses unknown tools — and "app_only" tools, which don't exist as far as the AI is concerned;
  2. refuses "gated" tools unless this run was specifically set up to resume an approved update;
  3. enforces a hard call budget (stops runaway loops);
  4. checks the arguments against a strict form (no extra fields);
  5. runs the tool, whose own code re-checks scope and ordering;
  6. WRITES TO THE AUDIT DIARY AUTOMATICALLY — success, denial or error — so the AI cannot skip or hide it;
  7. returns a size-capped, redacted result. It never raises errors at the AI; denials are just messages.
"""
import json

from pydantic import ValidationError

from org_automation.agent.context import RunContext
from org_automation.agent.tools import TOOLS, ToolDenied, ToolSpec
from org_automation.audit.writer import write_event
from org_automation.core.redaction import redact_text, scrub

MAX_RESULT_CHARS = 6000


def _visible(spec: ToolSpec, ctx: RunContext) -> bool:
    if spec.exposure == "app_only":
        return False
    if spec.exposure == "gated":
        return ctx.resume_approval_id is not None
    return True


def tool_schemas(ctx: RunContext) -> list[dict]:
    """The tool menu shown to the AI for this run (app-only and not-currently-allowed tools are absent)."""
    return [{"name": s.name, "description": s.description, "input_schema": s.args_model.model_json_schema()}
            for s in TOOLS.values() if _visible(s, ctx)]


def _short(args) -> dict:
    """Arguments safe to log: redact first, THEN truncate (cutting a number in half would defeat the redaction)."""
    clean = scrub(args if isinstance(args, dict) else {})
    return {k: (v[:200] if isinstance(v, str) else v) for k, v in clean.items()}


def call(ctx: RunContext, name: str, args: dict) -> dict:
    """Run one tool request. Always returns {"ok": bool, ...}; never raises."""
    outcome, reason, result = "ok", None, None
    spec = TOOLS.get(name)
    ctx.calls += 1
    try:
        if spec is None or not _visible(spec, ctx):
            outcome, reason = "unknown_tool", "No such tool."          # same answer for hidden and nonexistent tools
        elif ctx.calls > ctx.max_calls:
            outcome, reason = "denied", "Tool-call budget for this run is exhausted."
        else:
            try:
                parsed = spec.args_model.model_validate(args if isinstance(args, dict) else {})
            except ValidationError as exc:
                outcome, reason = "invalid_args", "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:300]
            else:
                try:
                    result = spec.handler(ctx, parsed)
                except ToolDenied as exc:
                    outcome, reason = "denied", str(exc)
                except Exception as exc:  # noqa: BLE001  (deliberate: report the type only, never internals)
                    outcome, reason = "error", type(exc).__name__
    finally:
        doc = args.get("document_id") if isinstance(args, dict) and isinstance(args.get("document_id"), str) else "n/a"
        write_event("tool_call", doc, outcome,
                    {"run_id": ctx.run_id, "tool": name, "args": scrub(_short(args)), "reason": reason},
                    actor=f"agent:{ctx.actor}")
        # One row per request: what the driver ASKED for (tool + arguments) next to what the gateway DECIDED.
        ctx.transcript.append({"step": len(ctx.transcript) + 1, "tool": name, "args": scrub(_short(args)),
                               "ok": outcome == "ok", "outcome": outcome, **({"why": reason} if reason else {})})

    if outcome != "ok":
        return {"ok": False, "error": reason}
    text = json.dumps(scrub(result))
    if len(text) > MAX_RESULT_CHARS:
        return {"ok": True, "result": {"truncated": True, "preview": redact_text(text[:MAX_RESULT_CHARS])}}
    return {"ok": True, "result": json.loads(text)}
