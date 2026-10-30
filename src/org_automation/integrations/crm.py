"""PLAIN ENGLISH: The connector to the firm's client database (CRM) — one "plug shape", two plugs.

`CrmAdapter` says what ANY CRM connection must do: look up a client, update a client, and answer "are you alive?".
Two interchangeable implementations:
  * MockCrmAdapter       – a pretend CRM with three fake clients (default; no setup, great for demos/tests)
  * SalesforceCrmAdapter – the real thing, in salesforce.py (turn on with CRM_PROVIDER=salesforce)
Nothing else in the app knows or cares which one is plugged in.

Every CRM handed out by get_crm() is wrapped in WriteGate, the single checkpoint for writes. In WRITE_MODE=shadow it
reads and checks like normal but refuses to write (and says so); in WRITE_MODE=enabled writes pass straight through.

Safety built into the plug shape:
  * Updates carry the values the reviewer SAW. If the CRM record changed since then, the update is refused
    (CrmConflict) instead of silently overwriting someone else's edit.
  * Repeating an update that already happened does nothing (idempotent), so retries are safe.
  * Failures are reported with a short, safe error — never tokens, URLs, or client data.
"""
import json
from datetime import UTC, datetime
from typing import ClassVar

from org_automation.core.settings import settings
from org_automation.storage.database import connect


class CrmError(RuntimeError):
    """Base for CRM problems. (A RuntimeError, so the API reports it as a retryable 502, not a crash.)"""


class CrmConflict(CrmError):
    """The record changed in the CRM after the reviewer looked at it; a human must re-review."""


class CrmUnavailable(CrmError):
    """The CRM couldn't be reached or refused our credentials. Safe to retry later."""


class CrmAmbiguous(CrmError):
    """More than one CRM record has this client number. Never guess which one."""


def plan_update(current: dict, changes: dict, expected: dict | None) -> bool:
    """Decide what an update should do. True = write needed; False = already applied (do nothing).
    Raises CrmConflict if a field differs from what the reviewer saw (`expected`) and isn't already the new value."""
    if all(current.get(k) == v for k, v in changes.items()):
        return False
    if expected is not None:
        for field in changes:
            if current.get(field) != expected.get(field):
                raise CrmConflict(f"Field '{field}' changed in the CRM after it was reviewed.")
    return True


class CrmAdapter:
    """Interface for a CRM; implementations must enforce authorization on every write."""

    def get_record(self, record_id: str) -> dict | None:
        """Return {'email': ..., 'effective_date': ...} for the client, or None if there is no such client."""
        raise NotImplementedError

    def update_record(self, record_id: str, changes: dict, idempotency_key: str,
                      expected_current: dict | None = None) -> dict:
        """Apply `changes`. Returns {'record_id': ..., 'applied': bool}; applied=False means it had already happened."""
        raise NotImplementedError

    def ping(self) -> bool:
        """A cheap 'can I reach the CRM and am I logged in?' check."""
        raise NotImplementedError


class MockCrmAdapter(CrmAdapter):
    """Pretend CRM for demos and tests. All data below is synthetic."""

    _RECORDS: ClassVar[dict] = {
        "C10482": {"name": "Demo Client One", "email": "old.client@example.com", "effective_date": "2025-01-01"},
        "C10001": {"name": "Demo Client Two", "email": "two@example.com", "effective_date": "2024-06-30"},
        "C20005": {"name": "Demo Client Three", "email": "three@example.com", "effective_date": "2026-03-15"},
    }

    def get_record(self, record_id: str) -> dict | None:
        rec = self._RECORDS.get(record_id)
        if rec is None:
            return None
        with connect() as db:  # show earlier approved updates so the demo "remembers" changes
            rows = db.execute("SELECT changes_json FROM mock_crm_updates WHERE record_id=? ORDER BY applied_at",
                              (record_id,)).fetchall()
        merged = dict(rec)
        for r in rows:
            merged.update(json.loads(r["changes_json"]))
        return merged

    def ping(self) -> bool:
        return True

    def update_record(self, record_id: str, changes: dict, idempotency_key: str,
                      expected_current: dict | None = None) -> dict:
        current = self.get_record(record_id)
        if current is None:
            raise KeyError(record_id)
        if not plan_update(current, changes, expected_current):
            return {"record_id": record_id, "applied": False}
        with connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO mock_crm_updates(idempotency_key, record_id, changes_json, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (idempotency_key, record_id, json.dumps(changes, sort_keys=True),
                 datetime.now(UTC).isoformat()))
        return {"record_id": record_id, "applied": cur.rowcount == 1}  # False = this exact update already happened


class WriteGate(CrmAdapter):
    """The ONE checkpoint every CRM write passes through (callers get it from get_crm()).

    shadow  : do everything except the write. We still read the record and run the same stale-data check, so the result
              says honestly what WOULD have happened ("would_apply") or raises the same conflict the real write would.
    enabled : hand the write to the real connector unchanged.
    The mode is read on every call, and is checked here rather than in each caller so no code path can forget it."""

    def __init__(self, inner: CrmAdapter):
        self.inner = inner

    def get_record(self, record_id: str) -> dict | None:
        return self.inner.get_record(record_id)

    def ping(self) -> bool:
        return self.inner.ping()

    def update_record(self, record_id: str, changes: dict, idempotency_key: str,
                      expected_current: dict | None = None) -> dict:
        if settings.effective_write_mode() == "enabled":
            return self.inner.update_record(record_id, changes, idempotency_key, expected_current)
        current = self.inner.get_record(record_id)
        if current is None:
            raise KeyError(record_id)
        would_apply = plan_update(current, changes, expected_current)    # raises CrmConflict exactly like a real write
        return {"record_id": record_id, "applied": False, "shadow": True, "would_apply": would_apply}


_salesforce = None   # one shared Salesforce adapter, so its login token is reused between calls


def get_crm() -> CrmAdapter:
    """Pick the CRM based on the CRM_PROVIDER setting, behind the write gate (see WRITE_MODE)."""
    global _salesforce
    if settings.crm_provider == "salesforce":
        if _salesforce is None:
            from org_automation.integrations.salesforce import SalesforceCrmAdapter
            _salesforce = SalesforceCrmAdapter.from_settings()
        return WriteGate(_salesforce)
    return WriteGate(MockCrmAdapter())


def reset_crm_cache() -> None:
    global _salesforce
    _salesforce = None
