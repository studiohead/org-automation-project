"""PLAIN ENGLISH: The scorecard. It answers "is this automation actually any good?" using the system's own records.

Most of it is counting what already happened (documents handled, exceptions and why, review decisions, how many tool requests
the gateway refused). The interesting part is the AGREEMENT CHECK, which is what makes a read-only trial worth running:

  For every change the system proposed, look at the client's record in the CRM NOW and compare it with what we proposed
  and with what the record held when we proposed it. That tells us what staff did on their own:
    agreed     - the record now holds exactly what we proposed (staff made the same change)      -> system was right
    untouched  - the record is still as it was (nobody has made the change yet)                  -> the backlog it would clear
    different  - staff changed a field to something OTHER than we proposed                       -> look at these first
    partial    - staff made some of the change but not all of it
  Changes this system wrote itself are set aside (applied_by_system): of course those match, so they prove nothing.

Nothing here writes anything. It only reads our own database and, to check agreement, reads CRM records.
"""
import json
import statistics
from datetime import UTC, datetime, timedelta

from org_automation.core.settings import settings
from org_automation.integrations.crm import CrmError, get_crm
from org_automation.storage.database import connect

MAX_PROPOSALS = 1000          # most recent proposals considered
MAX_DISAGREEMENTS_SHOWN = 20
MAX_CRM_LOOKUPS = 50          # distinct client records read per scorecard (protects the CRM's API limits)


def classify(baseline: dict, proposed: dict, now: dict) -> tuple[str, list[dict]]:
    """Compare the record now with what we proposed and with what it held when we proposed. Returns (verdict, differences)."""
    if all(baseline.get(k) == v for k, v in proposed.items()):
        return "no_change", []                                  # we proposed nothing new, so there is nothing to judge
    if all(now.get(k) == v for k, v in proposed.items()):
        return "agreed", []
    if all(now.get(k) == baseline.get(k) for k in proposed):
        return "untouched", []
    diffs = [{"field": k, "proposed": v, "crm_before": baseline.get(k), "crm_now": now.get(k)}
             for k, v in proposed.items() if now.get(k) != v and now.get(k) != baseline.get(k)]
    return ("different" if diffs else "partial"), diffs


def _minutes(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def build_scorecard(days: int = 30) -> dict:
    now = datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()
    with connect() as db:
        approvals = [dict(r) for r in db.execute(
            "SELECT approval_id, document_id, kind, status, proposal_json, created_at, decided_at FROM approvals "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?", (since, MAX_PROPOSALS)).fetchall()]
        events = [dict(r) for r in db.execute(
            "SELECT event_type, outcome, details_json, timestamp FROM audit_events "
            "WHERE event_type IN ('crm_update_applied','crm_update_shadowed','tool_call','agent_run_started',"
            "'duplicate_event','duplicate_webhook')").fetchall()]
    in_window = [e for e in events if e["timestamp"] >= since]
    system_wrote = {json.loads(e["details_json"]).get("approval_id") for e in events if e["event_type"] == "crm_update_applied"}

    # ---- volume and people
    changes = [a for a in approvals if a["kind"] == "change"]
    exceptions = [a for a in approvals if a["kind"] == "exception"]
    reasons: dict[str, int] = {}
    for a in exceptions:
        for code in {i["code"] for i in json.loads(a["proposal_json"]).get("issues", [])}:
            reasons[code] = reasons.get(code, 0) + 1
    decided = [a for a in approvals if a["decided_at"]]
    waits = [_minutes(a["created_at"], a["decided_at"]) for a in decided]
    count = lambda rows, **kw: sum(all(r[k] == v for k, v in kw.items()) for r in rows)

    # ---- the agreement check
    verdicts = {"agreed": 0, "untouched": 0, "different": 0, "partial": 0, "applied_by_system": 0, "unavailable": 0, "no_change": 0}
    disagreements: list[dict] = []
    records: dict[str, dict | None] = {}
    crm = get_crm()
    for a in changes:
        if a["status"] not in ("pending", "approved"):
            continue
        p = json.loads(a["proposal_json"])
        if a["approval_id"] in system_wrote:
            verdicts["applied_by_system"] += 1
            continue
        rid = p["record_id"]
        if rid not in records and len(records) >= MAX_CRM_LOOKUPS:
            verdicts["unavailable"] += 1      # over the lookup cap: counted honestly, not silently dropped
            continue
        if rid not in records:
            try:
                records[rid] = crm.get_record(rid)
            except CrmError:
                records[rid] = None
        if records[rid] is None:
            verdicts["unavailable"] += 1
            continue
        verdict, diffs = classify(p.get("crm_current") or {}, p["proposed_changes"], records[rid])
        verdicts[verdict] += 1
        if verdict == "different" and len(disagreements) < MAX_DISAGREEMENTS_SHOWN:
            disagreements.append({"approval_id": a["approval_id"], "document_id": a["document_id"], "record_id": rid,
                                  "status": a["status"], "differences": diffs})
    judged = verdicts["agreed"] + verdicts["different"] + verdicts["partial"]

    tool_calls = [e for e in in_window if e["event_type"] == "tool_call"]
    minutes = settings.manual_minutes_per_update
    live = [a for a in changes if a["status"] != "rejected"]
    return {
        "window_days": days, "generated_at": now.isoformat(), "write_mode": settings.effective_write_mode(),
        "volume": {"documents": len({a["document_id"] for a in approvals}), "changes_proposed": len(changes),
                   "exceptions": len(exceptions),
                   "duplicates_ignored": sum(e["event_type"] in ("duplicate_event", "duplicate_webhook") for e in in_window)},
        "exceptions_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "reviews": {"approved": count(approvals, status="approved"), "rejected": count(approvals, status="rejected"),
                    "pending": count(approvals, status="pending"),
                    "median_minutes_to_decision": round(statistics.median(waits), 1) if waits else None},
        "agent": {"runs": sum(e["event_type"] == "agent_run_started" for e in in_window), "tool_requests": len(tool_calls),
                  "refused": sum(e["outcome"] != "ok" for e in tool_calls)},
        "shadow": {"would_apply": sum(e["event_type"] == "crm_update_shadowed" and e["outcome"] == "would_apply" for e in in_window),
                   "already_current": sum(e["event_type"] == "crm_update_shadowed" and e["outcome"] == "already_current" for e in in_window)},
        "agreement": {**verdicts, "judged": judged, "rate": round(verdicts["agreed"] / judged, 3) if judged else None},
        "disagreements": disagreements,
        "estimate": ({"manual_minutes_per_update": minutes, "hours_of_manual_work_covered": round(len(live) * minutes / 60, 1)}
                     if minutes else None),
    }
