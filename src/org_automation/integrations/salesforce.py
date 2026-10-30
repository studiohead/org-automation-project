"""PLAIN ENGLISH: The real connection to Salesforce.

What it does, in everyday terms:
  1. LOGS IN with a "valet key" (OAuth client-credentials): the app proves who it is to Salesforce and gets a
     short-lived access token. The token lives only in memory — never in logs, the database, or anything the AI sees.
  2. LOOKS UP a client by the firm's own client number, stored in a custom Salesforce field (default `Client_ID__c`).
     It asks Salesforce for ONLY the two fields we need (least data). Zero matches = "no such client";
     two or more = "ambiguous" (we never guess which one is right).
  3. UPDATES the client, but only after re-reading the record: if it already has the new values, nothing is written
     (safe retries); if it differs from what the human reviewer saw, we refuse (CrmConflict) rather than overwrite
     a colleague's edit.
Safety rails: only talks to *.salesforce.com / *.force.com over HTTPS, refuses redirects, short timeouts, and
turns every failure into a short, safe error (type and status only — never tokens, URLs or client data).

Known limits: a tiny window remains between "re-read" and "write" (Salesforce's REST API has no compare-and-set);
the integration user's field-level permissions are the backstop. Salesforce API names and flows below follow
Salesforce's REST/OAuth docs as I understand them — verify against your org and the current docs
(see docs/SALESFORCE_SETUP.md). Untested against a live org; tested against a simulated one.
"""
import re
import threading
from urllib.parse import urlparse

import httpx

from org_automation.core.settings import settings
from org_automation.integrations.crm import (
    CrmAdapter,
    CrmAmbiguous,
    CrmError,
    CrmUnavailable,
    plan_update,
)

_API_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,60}")
_CLIENT_NUMBER = re.compile(r"[A-Za-z0-9_-]{1,50}")
_VERSION = re.compile(r"v\d{2}\.0")
_ALLOWED_HOST_SUFFIXES = (".salesforce.com", ".force.com")


def _check_https_salesforce_url(url: str, what: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith(_ALLOWED_HOST_SUFFIXES) or parsed.username or parsed.port not in (None, 443):
        raise CrmError(f"{what} must be an https URL on salesforce.com or force.com.")
    return f"https://{host}"


class SalesforceCrmAdapter(CrmAdapter):
    def __init__(self, *, domain: str, client_id: str, client_secret: str, api_version: str = "v62.0",
                 sobject: str = "Contact", client_id_field: str = "Client_ID__c", email_field: str = "Email",
                 effective_date_field: str = "Email_Effective_Date__c", timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        for name in (sobject, client_id_field, email_field, effective_date_field):
            if not _API_NAME.fullmatch(name):
                raise CrmError("Invalid Salesforce object/field API name in configuration.")
        if not _VERSION.fullmatch(api_version):
            raise CrmError("Salesforce API version must look like 'v62.0'.")
        if not client_id or not client_secret:
            raise CrmError("Salesforce client id and secret are required.")
        self._login_url = _check_https_salesforce_url(domain, "SALESFORCE_DOMAIN")
        self._client_id, self._client_secret = client_id, client_secret
        self._version, self._sobject = api_version, sobject
        self._id_field = client_id_field
        # our friendly names  ->  this org's Salesforce field API names
        self._fields = {"email": email_field, "effective_date": effective_date_field}
        self._http = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)
        self._token: str | None = None
        self._base: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls) -> "SalesforceCrmAdapter":
        secret = settings.salesforce_client_secret.get_secret_value() if settings.salesforce_client_secret else ""
        return cls(domain=settings.salesforce_domain, client_id=settings.salesforce_client_id, client_secret=secret,
                   api_version=settings.salesforce_api_version, sobject=settings.salesforce_object,
                   client_id_field=settings.salesforce_client_id_field, email_field=settings.salesforce_email_field,
                   effective_date_field=settings.salesforce_effective_date_field,
                   timeout=settings.salesforce_timeout_seconds)

    # ---------------------------------------------------------------- login
    def _login(self) -> None:
        try:
            r = self._http.post(f"{self._login_url}/services/oauth2/token", data={
                "grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret})
        except httpx.HTTPError as exc:
            raise CrmUnavailable(f"Salesforce login failed ({type(exc).__name__}).") from exc
        if r.status_code != 200:
            raise CrmUnavailable(f"Salesforce login was refused (HTTP {r.status_code}).")
        try:
            body = r.json()
            token, instance = body["access_token"], body["instance_url"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CrmUnavailable("Salesforce login returned an unexpected response.") from exc
        self._base = _check_https_salesforce_url(instance, "Salesforce instance_url")   # never send the token elsewhere
        self._token = token

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One authenticated call. Logs in on demand; if the token was rejected (401), logs in once more and retries."""
        for attempt in (1, 2):
            with self._lock:
                if self._token is None:
                    self._login()
                token, base = self._token, self._base
            try:
                r = self._http.request(method, f"{base}/services/data/{self._version}{path}",
                                       headers={"Authorization": f"Bearer {token}"}, **kwargs)
            except httpx.HTTPError as exc:
                raise CrmUnavailable(f"Salesforce request failed ({type(exc).__name__}).") from exc
            if r.status_code == 401 and attempt == 1:
                with self._lock:
                    self._token = None
                continue
            return r
        raise CrmUnavailable("Salesforce rejected our credentials.")   # pragma: no cover (loop always returns/raises)

    @staticmethod
    def _error_code(r: httpx.Response) -> str:
        try:
            return str(r.json()[0].get("errorCode", "UNKNOWN"))[:60]   # the code only; messages may echo client data
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return "UNKNOWN"

    def _fail(self, r: httpx.Response, action: str) -> CrmError:
        if r.status_code >= 500 or r.status_code in (401, 429):
            return CrmUnavailable(f"Salesforce {action} unavailable (HTTP {r.status_code}).")
        return CrmError(f"Salesforce {action} rejected (HTTP {r.status_code}, {self._error_code(r)}).")

    # ---------------------------------------------------------------- the CrmAdapter interface
    def ping(self) -> bool:
        r = self._request("GET", "/")
        if r.status_code != 200:
            raise self._fail(r, "health check")
        return True

    def _lookup(self, record_id: str) -> tuple[str, dict] | None:
        """Find the one record with this client number. Returns (Salesforce Id, our-named fields) or None."""
        if not _CLIENT_NUMBER.fullmatch(record_id):
            return None                                  # can't be a real client number; also keeps the query safe
        cols = ", ".join(["Id", *self._fields.values()])  # ask for ONLY what we need
        soql = f"SELECT {cols} FROM {self._sobject} WHERE {self._id_field} = '{record_id}' LIMIT 2"
        r = self._request("GET", "/query", params={"q": soql})
        if r.status_code != 200:
            raise self._fail(r, "lookup")
        try:
            records = r.json()["records"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CrmError("Salesforce lookup returned an unexpected response.") from exc
        if not records:
            return None
        if len(records) > 1:
            raise CrmAmbiguous("More than one Salesforce record has this client number.")
        rec = records[0]
        return rec["Id"], {ours: rec.get(theirs) for ours, theirs in self._fields.items()}

    def get_record(self, record_id: str) -> dict | None:
        found = self._lookup(record_id)
        return found[1] if found else None

    def update_record(self, record_id: str, changes: dict, idempotency_key: str,
                      expected_current: dict | None = None) -> dict:
        unknown = set(changes) - set(self._fields)
        if unknown:
            raise CrmError(f"Refusing to write unmapped fields: {sorted(unknown)}")   # only email / effective_date, ever
        found = self._lookup(record_id)
        if found is None:
            raise KeyError(record_id)
        sf_id, current = found
        if not plan_update(current, changes, expected_current):   # already applied -> safe no-op (retries are harmless)
            return {"record_id": record_id, "applied": False}
        body = {self._fields[k]: v for k, v in changes.items()}
        r = self._request("PATCH", f"/sobjects/{self._sobject}/{sf_id}", json=body)
        if r.status_code != 204:
            raise self._fail(r, "update")
        return {"record_id": record_id, "applied": True}
