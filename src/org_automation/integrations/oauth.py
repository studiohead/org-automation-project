"""PLAIN ENGLISH: The "valet key" fetcher for connecting to other systems.

Modern systems (Microsoft, DocuSign, banks) don't take passwords; they hand out short-lived access tokens through a
standard called OAuth. This file gets them. Rules baked in:
  * Tokens live in memory only, are reused until shortly before they expire, and are NEVER logged or shown to the AI.
  * Errors carry a short status only: never the secret, the token, or the provider's response text.
  * The login URL is built from a validated tenant ID and always points at Microsoft's login host, so the client secret
    can only ever be sent to Microsoft.
Production note: prefer a certificate or a managed identity over a client secret.
"""
import re
import threading
import time
from typing import Protocol

import httpx

_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")   # a tenant GUID or a verified domain name
_LOGIN_HOST = "https://login.microsoftonline.com"
_EXPIRY_MARGIN_SECONDS = 60


class TokenError(RuntimeError):
    """Could not get a token. A RuntimeError, so the API reports it as a retryable 502."""


class TokenProvider(Protocol):
    def get_access_token(self, audience: str, scopes: list[str] | None = None) -> str:
        """Return a valid access token for `audience`. Never log it; never pass it to a model."""
        ...

    def invalidate(self) -> None:
        """Forget the cached token (call after the service rejected it)."""
        ...


class ClientCredentialsTokenProvider:
    """Microsoft Entra ID, OAuth 2.0 client-credentials flow (the app signs in as itself, not as a person)."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str,
                 transport: httpx.BaseTransport | None = None, timeout: float = 10.0, clock=time.monotonic):
        if not _TENANT.fullmatch(tenant_id or "") or not client_id or not client_secret:
            raise TokenError("Microsoft tenant id, client id and client secret are required and must be well formed.")
        self._url = f"{_LOGIN_HOST}/{tenant_id}/oauth2/v2.0/token"
        self._client_id, self._secret = client_id, client_secret
        self._http = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)
        self._clock = clock
        self._cache: dict[str, tuple[str, float]] = {}      # scope -> (token, expires_at)
        self._lock = threading.Lock()

    def get_access_token(self, audience: str, scopes: list[str] | None = None) -> str:
        scope = " ".join(scopes) if scopes else f"{audience.rstrip('/')}/.default"
        with self._lock:
            cached = self._cache.get(scope)
            if cached and self._clock() < cached[1]:
                return cached[0]
            try:
                r = self._http.post(self._url, data={"grant_type": "client_credentials", "client_id": self._client_id,
                                                     "client_secret": self._secret, "scope": scope})
            except httpx.HTTPError as exc:
                raise TokenError(f"Microsoft sign-in failed ({type(exc).__name__}).") from exc
            if r.status_code != 200:
                raise TokenError(f"Microsoft sign-in was refused (HTTP {r.status_code}).")
            try:
                body = r.json()
                token, lifetime = str(body["access_token"]), float(body.get("expires_in", 300))
            except (ValueError, KeyError, TypeError) as exc:
                raise TokenError("Microsoft sign-in returned an unexpected response.") from exc
            self._cache[scope] = (token, self._clock() + max(lifetime - _EXPIRY_MARGIN_SECONDS, 0))
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
