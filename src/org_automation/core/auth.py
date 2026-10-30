"""PLAIN ENGLISH: The bouncer at the door.

Every request must prove who is calling, in one of two ways (AUTH_MODE decides which are accepted):
  * Microsoft Entra ID sign-in: `Authorization: Bearer <token>`. People sign in with their work account; automation uses
    an app-only token. See core/entra.py. This is the production path.
  * A shared secret in the `X-API-Key` header (AUTH_MODE=api_key or hybrid). Demo-grade only.
Either way the caller gets a name (so we know WHO did something; it is written into the audit log) and a role:
  - "service":  automation robots that submit documents
  - "reviewer": humans who approve/reject proposed changes
API keys are compared in a way that doesn't leak timing hints. A token or key that is rejected never says why.
"""
import hmac
import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from org_automation.core import entra
from org_automation.core.settings import settings

_log = logging.getLogger("org_automation.auth")


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    subject: str = ""            # stable id of the person/app (Entra object id); empty for shared API keys
    auth: str = "api_key"        # api_key | entra_user | entra_app


def uses_entra() -> bool:
    return settings.auth_mode in ("entra", "hybrid")


def uses_api_keys() -> bool:
    return settings.auth_mode in ("api_key", "hybrid")


def parse_keys(raw: str) -> list[tuple[str, Principal]]:
    entries = []
    for chunk in filter(None, (c.strip() for c in raw.split(","))):
        parts = chunk.split(":")
        if len(parts) == 3 and all(parts):
            name, secret, role = parts
            entries.append((secret, Principal(name=name, role=role)))
    return entries


def _unauthorized(detail: str = "Invalid or missing credentials.") -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if uses_entra() else None
    return HTTPException(status_code=401, detail=detail, headers=headers)


def _from_bearer(authorization: str) -> Principal:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized()
    try:
        who = entra.get_validator().validate(token.strip())
    except entra.EntraUnavailable as exc:
        _log.error("Entra signing keys unavailable: %s", exc.reason)
        raise HTTPException(status_code=503, detail="Sign-in service is temporarily unavailable.") from exc
    except entra.EntraAuthError as exc:
        _log.warning("Entra token rejected: %s", exc.reason)           # the reason stays in the server log only
        raise _unauthorized("Invalid or expired token.") from exc
    return Principal(name=who.name, role=who.role, subject=who.subject,
                     auth="entra_user" if who.kind == "user" else "entra_app")


def _from_api_key(x_api_key: str) -> Principal:
    found = None
    for secret, principal in parse_keys(settings.api_keys):  # check ALL keys: constant-ish time
        if hmac.compare_digest(secret.encode(), x_api_key.encode()):
            found = principal
    if found is None:
        raise _unauthorized("Invalid API key.")
    return found


def require_role(*roles: str):
    """Build a FastAPI dependency that only lets through callers whose identity has one of `roles`."""

    def dependency(authorization: str | None = Header(default=None),
                   x_api_key: str | None = Header(default=None)) -> Principal:
        if authorization:
            if not uses_entra():                       # never fall through to another method: a wrong credential is an error
                raise _unauthorized("Bearer tokens are not accepted by this server.")
            who = _from_bearer(authorization)
        elif x_api_key:
            if not uses_api_keys():
                raise _unauthorized("API keys are disabled; sign in with Microsoft.")
            who = _from_api_key(x_api_key)
        else:
            raise _unauthorized("Missing credentials.")
        if who.role not in roles:
            raise HTTPException(status_code=403, detail="This identity is not allowed to do that.")
        return who

    return Depends(dependency)


def validate_auth_config() -> None:
    """Called at startup. Stop early, with a plain message, if the chosen sign-in mode can't work."""
    if uses_entra():
        entra.validate_config(settings.entra_tenant_id, settings.entra_api_client_id, settings.entra_group_roles)


def demo_logins() -> list[dict]:
    """For the demo web page only: name, role and key of every login whose secret starts with "demo-".
    Real secrets can never appear here, even if this is called by mistake."""
    if not uses_api_keys():
        return []
    return [{"name": who.name, "role": who.role, "key": secret}
            for secret, who in parse_keys(settings.api_keys) if secret.startswith("demo-")]
