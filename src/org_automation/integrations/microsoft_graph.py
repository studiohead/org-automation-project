"""PLAIN ENGLISH: The connector to Microsoft 365 / SharePoint (where the firm files its documents).

After a person approves a client change and the CRM step is done, the app files a small APPROVAL RECORD (what was
proposed, who approved it, when, and the note) in one SharePoint folder. That gives compliance a copy outside this
app's own database.

Everything here follows the same rules as the Salesforce adapter:
  * Least privilege: the app is meant to hold `Sites.Selected` on ONE site, not the whole tenant.
  * The token only ever goes to graph.microsoft.com (https, no redirects), checked by `check_graph_url`.
  * Safe to retry: the file name comes from the approval, and "already exists" is treated as "already filed".
  * Safe failures: errors say the HTTP status and type only, never tokens, URLs, file names or client data.
  * Writes are controlled by the same WRITE_MODE gate as the CRM (see workflows/archive_record.py).
`MockDocumentArchive` stands in for SharePoint in the demo and tests; nothing else in the app knows which is plugged in.
Known limits: small-file upload only (the records are tiny); the Graph endpoints below follow Microsoft's documentation
as I understand it. Tested against simulated Graph responses, NOT a live tenant: confirm with your M365 admin.
"""
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote, urlparse

import httpx

from org_automation.core.settings import settings
from org_automation.integrations.oauth import ClientCredentialsTokenProvider, TokenProvider
from org_automation.storage.database import connect

GRAPH_HOSTS = frozenset({"graph.microsoft.com", "login.microsoftonline.com"})
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_AUDIENCE = "https://graph.microsoft.com"

_SITE_ID = re.compile(r"[A-Za-z0-9.,_-]{1,200}")          # e.g. contoso.sharepoint.com,<guid>,<guid>
_FOLDER_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,60}")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class GraphError(RuntimeError):
    """Base for Graph problems. A RuntimeError, so the API reports it as a retryable 502 like the CRM errors do."""


class GraphUnavailable(GraphError):
    """Microsoft could not be reached, or throttled us. Safe to retry later."""


def check_graph_url(url: str, what: str = "Microsoft Graph URL") -> str:
    """Return the URL only if it is https on an allowed Microsoft host with no embedded credentials or odd port."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in GRAPH_HOSTS or parsed.username or parsed.port not in (None, 443):
        raise GraphError(f"{what} must be an https URL on graph.microsoft.com or login.microsoftonline.com.")
    return url


def safe_file_name(document_id: str, discriminator: str, extension: str = "json") -> str:
    """A file name built only from safe characters: no slashes, no dots-only names, bounded length."""
    def clean(text: str) -> str:
        return re.sub(r"\.{2,}", "_", _UNSAFE_NAME.sub("_", text))     # no "..", no slashes, nothing but safe characters
    stem = clean(document_id).strip("._") or "document"
    return f"{stem[:80]}-{clean(discriminator).strip('._')[:36]}.{extension}"


@dataclass(frozen=True)
class ArchiveReceipt:
    """Proof a record was filed. Opaque ID only; no content, file name, client data or links."""
    drive_item_id: str
    already_existed: bool = False        # True when a retry found the earlier copy instead of creating another


class DocumentArchive(Protocol):
    def archive_record(self, document_id: str, discriminator: str, content: bytes) -> ArchiveReceipt:
        """File a record. Calling twice with the same document_id + discriminator must not create a second copy."""
        ...

    def ping(self) -> bool:
        """Can we reach the archive and are we allowed in?"""
        ...


class MockDocumentArchive:
    """Pretend SharePoint for demos and tests: records go into the local database."""

    def archive_record(self, document_id: str, discriminator: str, content: bytes) -> ArchiveReceipt:
        name = safe_file_name(document_id, discriminator)
        with connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO mock_archive(file_name, document_id, content, created_at) VALUES (?,?,?,?)",
                             (name, document_id, content.decode("utf-8", "replace"), datetime.now(UTC).isoformat()))
        return ArchiveReceipt(drive_item_id=f"mock:{name}", already_existed=cur.rowcount == 0)

    def ping(self) -> bool:
        return True


class GraphSharePointAdapter:
    """Files approval records in ONE SharePoint site's default document library, under one folder."""

    def __init__(self, *, site_id: str, folder: str, tokens: TokenProvider, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None, base_url: str = GRAPH_BASE):
        if not _SITE_ID.fullmatch(site_id or ""):
            raise GraphError("GRAPH_SITE_ID is missing or malformed.")
        segments = [s for s in (folder or "").split("/") if s]
        if not segments or not all(_FOLDER_SEGMENT.fullmatch(s) for s in segments):
            raise GraphError("GRAPH_FOLDER must be plain folder names separated by '/'.")
        self._site = site_id
        self._folder = "/".join(quote(s) for s in segments)
        self._base = check_graph_url(base_url, "Graph base URL").rstrip("/")
        self._tokens = tokens
        self._http = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)

    @classmethod
    def from_settings(cls) -> "GraphSharePointAdapter":
        secret = settings.graph_client_secret.get_secret_value() if settings.graph_client_secret else ""
        tokens = ClientCredentialsTokenProvider(tenant_id=settings.graph_tenant_id, client_id=settings.graph_client_id,
                                                client_secret=secret)
        return cls(site_id=settings.graph_site_id, folder=settings.graph_folder, tokens=tokens,
                   timeout=settings.graph_timeout_seconds)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One authenticated call. If the token is rejected (401) fetch a fresh one and try once more."""
        for attempt in (1, 2):
            token = self._tokens.get_access_token(GRAPH_AUDIENCE)
            try:
                r = self._http.request(method, f"{self._base}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
            except httpx.HTTPError as exc:
                raise GraphUnavailable(f"Microsoft Graph request failed ({type(exc).__name__}).") from exc
            if r.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            return r
        raise GraphUnavailable("Microsoft Graph rejected our credentials.")   # pragma: no cover

    @staticmethod
    def _fail(r: httpx.Response, action: str) -> GraphError:
        if r.status_code >= 500 or r.status_code in (401, 429):
            return GraphUnavailable(f"Graph {action} unavailable (HTTP {r.status_code}).")
        return GraphError(f"Graph {action} rejected (HTTP {r.status_code}).")   # status only; bodies may echo names

    def ping(self) -> bool:
        r = self._request("GET", f"/sites/{quote(self._site, safe=',.')}", params={"$select": "id"})
        if r.status_code != 200:
            raise self._fail(r, "health check")
        return True

    def archive_record(self, document_id: str, discriminator: str, content: bytes) -> ArchiveReceipt:
        name = quote(safe_file_name(document_id, discriminator))
        item = f"/sites/{quote(self._site, safe=',.')}/drive/root:/{self._folder}/{name}"
        # conflictBehavior=fail: a retry can never overwrite or duplicate; "already there" is a success we confirm below.
        r = self._put(item, content)
        if r.status_code in (200, 201):
            return ArchiveReceipt(drive_item_id=self._item_id(r), already_existed=False)
        if r.status_code == 409:
            existing = self._request("GET", item, params={"$select": "id"})
            if existing.status_code == 200:
                return ArchiveReceipt(drive_item_id=self._item_id(existing), already_existed=True)
            raise self._fail(existing, "archive lookup")
        raise self._fail(r, "archive upload")

    def _put(self, item: str, content: bytes) -> httpx.Response:
        return self._request("PUT", f"{item}:/content", params={"@microsoft.graph.conflictBehavior": "fail"},
                             content=content)

    @staticmethod
    def _item_id(r: httpx.Response) -> str:
        try:
            return str(r.json()["id"])[:200]
        except (ValueError, KeyError, TypeError) as exc:
            raise GraphError("Graph returned an unexpected response.") from exc


_adapter: GraphSharePointAdapter | None = None   # one shared adapter, so its token cache is reused between calls


def get_archive() -> DocumentArchive | None:
    """The configured archive, or None when ARCHIVE_PROVIDER=none (the default)."""
    global _adapter
    if settings.archive_provider == "mock":
        return MockDocumentArchive()
    if settings.archive_provider == "sharepoint":
        if _adapter is None:
            _adapter = GraphSharePointAdapter.from_settings()
        return _adapter
    return None


def reset_archive_cache() -> None:
    global _adapter
    _adapter = None
