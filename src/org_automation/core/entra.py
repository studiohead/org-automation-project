"""PLAIN ENGLISH: Checks a Microsoft Entra ID sign-in token and says who the caller is and what they may do.

When a person signs in with their work account, Microsoft hands their browser a signed "access token". The browser
shows it to this API; this file proves it is genuine and meant for us, without ever calling Microsoft per request.

What is checked, in order (any failure = rejected, and the caller only ever sees "invalid or expired token"):
  1. Signature: RS256 only, verified against Microsoft's published signing keys for OUR tenant (the key URL is built from
     the configured tenant, never taken from the token). "none"/HS256 tokens are refused.
  2. Issuer is our tenant's v2.0 endpoint, tenant id matches, token version is 2.0.
  3. Audience is this API's app registration. Not expired, not used before its start time (60 s clock leeway).
  4. Who and what:
       * A PERSON's token (it carries a delegated scope): must include the scope `access_as_user`, and can only ever
         become a `reviewer` (via an Entra app role "Reviewer", or a mapped directory group).
       * An AUTOMATION's token (app-only, no scope): can only ever become `service` (via the app role "Service").
     This keeps the four-eyes idea intact at the identity layer: a human account cannot submit as automation, and an
     automation account cannot approve.
No role = no access (fail closed). Group lists too long for a token ("overage") carry no groups here, so they grant
nothing; use app roles, which are never truncated.
Needs: PyJWT with the cryptography extra. Tested with locally generated keys, not against a live tenant.
"""
import logging
import re
import threading
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWTError

_log = logging.getLogger("org_automation.auth")
_LOGIN = "https://login.microsoftonline.com"
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SAFE_NAME = re.compile(r"[A-Za-z0-9@._+-]{1,120}")
ROLES = ("reviewer", "service")


class EntraAuthError(Exception):
    """Token rejected. `reason` is a short machine code for server logs; it is never sent to the caller."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EntraUnavailable(EntraAuthError):
    """Microsoft's signing keys could not be fetched (and we have none cached): a 503, not a bad token."""


@dataclass(frozen=True)
class EntraIdentity:
    name: str          # who, as written in the audit log (never empty, never contains ':' except the 'app:' / 'user:' prefix)
    role: str          # reviewer | service
    subject: str       # the stable object id of the person or app
    kind: str          # user | app


def parse_group_roles(raw: str) -> dict[str, str]:
    """'<group-guid>:reviewer,<group-guid>:reviewer' -> {guid: role}. Raises ValueError on anything unclear."""
    out: dict[str, str] = {}
    for chunk in filter(None, (c.strip() for c in (raw or "").split(","))):
        gid, _, role = chunk.partition(":")
        if not _GUID.fullmatch(gid) or role.lower() != "reviewer":
            raise ValueError("ENTRA_GROUP_ROLES entries must look like '<group-object-id>:reviewer' "
                             "(only people can be mapped from groups; automation uses the app role 'Service').")
        out[gid.lower()] = "reviewer"
    return out


def validate_config(tenant_id: str, api_client_id: str, group_roles: str) -> None:
    """Fail at startup, not at first sign-in, if the settings can't possibly work."""
    if not _TENANT.fullmatch(tenant_id or "") or not _GUID.fullmatch(api_client_id or ""):
        raise RuntimeError("AUTH_MODE needs ENTRA_TENANT_ID (tenant id or domain) and ENTRA_API_CLIENT_ID (a GUID).")
    try:
        parse_group_roles(group_roles)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


class EntraTokenValidator:
    def __init__(self, *, tenant_id: str, api_client_id: str, required_scope: str = "access_as_user",
                 group_roles: str = "", signing_key_provider=None, leeway_seconds: int = 60):
        validate_config(tenant_id, api_client_id, group_roles)
        self._tenant = tenant_id
        self._issuers = {f"{_LOGIN}/{tenant_id}/v2.0"}
        self._audiences = [api_client_id, f"api://{api_client_id}"]
        self._scope = required_scope
        self._groups = parse_group_roles(group_roles)
        self._leeway = leeway_seconds
        if signing_key_provider is None:
            client = PyJWKClient(f"{_LOGIN}/{tenant_id}/discovery/v2.0/keys", cache_keys=True, lifespan=3600, timeout=5)

            def signing_key_provider(token: str):
                return client.get_signing_key_from_jwt(token).key
        self._signing_key = signing_key_provider

    def validate(self, token: str) -> EntraIdentity:
        try:
            if jwt.get_unverified_header(token).get("alg") != "RS256":
                raise EntraAuthError("bad_algorithm")
            key = self._signing_key(token)
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self._audiences, issuer=list(self._issuers),
                                leeway=self._leeway, options={"require": ["exp", "iat", "iss", "aud"]})
        except EntraAuthError:
            raise
        except PyJWKClientConnectionError as exc:
            raise EntraUnavailable("jwks_unreachable") from exc
        except PyJWTError as exc:
            raise EntraAuthError(type(exc).__name__) from exc
        # The issuer already pins our tenant. When the tenant is configured as a GUID, also require the tid claim to match.
        if _GUID.fullmatch(self._tenant) and str(claims.get("tid", "")).lower() != self._tenant.lower():
            raise EntraAuthError("wrong_tenant")
        if claims.get("ver") != "2.0":
            raise EntraAuthError("not_v2_token")
        return self._identity(claims)

    def _identity(self, claims: dict) -> EntraIdentity:
        oid = str(claims.get("oid") or claims.get("sub") or "")
        if not oid:
            raise EntraAuthError("no_subject")
        app_roles = {str(r).lower() for r in (claims.get("roles") or []) if isinstance(r, str)}
        if "scp" in claims:                                            # a signed-in PERSON
            scopes = str(claims.get("scp") or "").split()
            if self._scope not in scopes:
                raise EntraAuthError("missing_scope")
            groups = {str(g).lower() for g in (claims.get("groups") or []) if isinstance(g, str)}
            mapped = {self._groups[g] for g in groups if g in self._groups}
            if "reviewer" not in (app_roles | mapped):
                raise EntraAuthError("no_role")                        # includes "has Service role but is a human"
            for candidate in (claims.get("preferred_username"), claims.get("upn"), claims.get("email")):
                if isinstance(candidate, str) and _SAFE_NAME.fullmatch(candidate.strip()):
                    return EntraIdentity(candidate.strip().lower(), "reviewer", oid, "user")
            return EntraIdentity(f"user:{oid}", "reviewer", oid, "user")
        azp = str(claims.get("azp") or claims.get("appid") or "")      # an AUTOMATION (app-only token)
        if "service" not in app_roles or not _GUID.fullmatch(azp):
            raise EntraAuthError("no_role")
        return EntraIdentity(f"app:{azp.lower()}", "service", oid, "app")


_validator: EntraTokenValidator | None = None
_validator_key: tuple | None = None
_lock = threading.Lock()


def get_validator() -> EntraTokenValidator:
    """One shared validator (so Microsoft's signing keys are cached), rebuilt if the settings change."""
    from org_automation.core.settings import settings
    global _validator, _validator_key
    key = (settings.entra_tenant_id, settings.entra_api_client_id, settings.entra_required_scope, settings.entra_group_roles)
    with _lock:
        if _validator is None or _validator_key != key:
            _validator = EntraTokenValidator(tenant_id=settings.entra_tenant_id, api_client_id=settings.entra_api_client_id,
                                             required_scope=settings.entra_required_scope,
                                             group_roles=settings.entra_group_roles)
            _validator_key = key
        return _validator
