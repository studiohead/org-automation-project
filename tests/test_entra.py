"""PLAIN ENGLISH: Checks for Microsoft Entra ID sign-in, using tokens signed with a throw-away key made inside the test.

Proves: a genuine token for the right tenant/audience gets the right role; every kind of forged, expired, mis-addressed or
wrongly-roled token is refused; people can only ever be reviewers and automation only ever service (so the four-eyes rule
holds at the identity layer); each AUTH_MODE accepts exactly what it should; and the sign-in settings leak no secrets."""
import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from org_automation.core import entra
from org_automation.core.auth import validate_auth_config
from org_automation.core.settings import settings
from org_automation.main import app

TENANT = "11111111-2222-3333-4444-555555555555"
API_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SPA_ID = "99999999-8888-7777-6666-555555555555"
AUTOMATION_APP = "cccccccc-1111-2222-3333-444444444444"
GROUP = "dddddddd-1111-2222-3333-444444444444"
ISS = f"https://login.microsoftonline.com/{TENANT}/v2.0"

PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
client = TestClient(app)


def token(key=PRIV, alg="RS256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": f"api://{API_ID}", "tid": TENANT, "ver": "2.0", "iat": now, "nbf": now - 5, "exp": now + 3600,
              "oid": "0a0a0a0a-0000-0000-0000-000000000001", "scp": "access_as_user", "roles": ["Reviewer"],
              "preferred_username": "Alice@Contoso.com"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "k1"})


def app_token(**over):
    base = {"scp": None, "roles": ["Service"], "azp": AUTOMATION_APP, "preferred_username": None,
            "oid": "0b0b0b0b-0000-0000-0000-000000000002"}
    base.update(over)
    return token(**base)


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(autouse=True)
def entra_on(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "entra")
    monkeypatch.setattr(settings, "entra_tenant_id", TENANT)
    monkeypatch.setattr(settings, "entra_api_client_id", API_ID)
    monkeypatch.setattr(settings, "entra_spa_client_id", SPA_ID)
    monkeypatch.setattr(settings, "entra_group_roles", f"{GROUP}:reviewer")
    validator = entra.EntraTokenValidator(tenant_id=TENANT, api_client_id=API_ID, group_roles=settings.entra_group_roles,
                                          signing_key_provider=lambda _t: PRIV.public_key())
    monkeypatch.setattr(entra, "get_validator", lambda: validator)


# ------------------------------------------------------------------ the happy paths
def test_person_with_reviewer_app_role_is_a_reviewer_named_by_their_sign_in():
    r = client.get("/me", headers=bearer(token()))
    assert r.status_code == 200 and r.json() == {"name": "alice@contoso.com", "role": "reviewer", "auth": "entra_user"}


def test_person_in_a_mapped_directory_group_is_a_reviewer():
    r = client.get("/me", headers=bearer(token(roles=None, groups=[GROUP.upper()])))
    assert r.status_code == 200 and r.json()["role"] == "reviewer"


def test_automation_with_service_app_role_is_service():
    r = client.get("/me", headers=bearer(app_token()))
    assert r.json() == {"name": f"app:{AUTOMATION_APP}", "role": "service", "auth": "entra_app"}


def test_audience_may_be_the_bare_client_id():
    assert client.get("/me", headers=bearer(token(aud=API_ID))).status_code == 200


def test_unsafe_display_name_falls_back_to_the_object_id():
    r = client.get("/me", headers=bearer(token(preferred_username="agent:evil <script>", oid="0a0a0a0a-0000-0000-0000-00000000000f")))
    assert r.json()["name"] == "user:0a0a0a0a-0000-0000-0000-00000000000f"


# ------------------------------------------------------------------ roles: people review, automation submits, never the reverse
@pytest.mark.parametrize("claims", [
    {"roles": ["Service"]},                      # a human holding the automation role is NOT automation
    {"roles": None},                             # signed in, but nobody gave them a role
    {"roles": ["Reviewer"], "scp": "other"},     # token not issued for this API's scope
    {"roles": None, "groups": ["eeeeeeee-1111-2222-3333-444444444444"]},   # a group we did not map
])
def test_people_without_the_reviewer_role_or_scope_are_refused(claims):
    assert client.get("/me", headers=bearer(token(**claims))).status_code == 401


def test_automation_cannot_hold_the_reviewer_role():
    assert client.get("/me", headers=bearer(app_token(roles=["Reviewer"]))).status_code == 401
    assert client.get("/me", headers=bearer(app_token(roles=None))).status_code == 401


def test_group_overage_grants_nothing():
    # Too many groups: Entra omits the list and sends a pointer instead. We never chase it: no role, no access.
    t = token(roles=None, groups=None, _claim_names={"groups": "src1"})
    assert client.get("/me", headers=bearer(t)).status_code == 401


# ------------------------------------------------------------------ forged / expired / mis-addressed tokens
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forged(alg: str, secret: bytes = b"") -> str:
    head = _b64(json.dumps({"alg": alg, "typ": "JWT", "kid": "k1"}).encode())
    now = int(time.time())
    body = _b64(json.dumps({"iss": ISS, "aud": API_ID, "tid": TENANT, "ver": "2.0", "iat": now, "exp": now + 600,
                            "scp": "access_as_user", "roles": ["Reviewer"], "oid": "x"}).encode())
    sig = hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest() if alg == "HS256" else b""
    return f"{head}.{body}.{_b64(sig)}"


@pytest.mark.parametrize("bad", [
    lambda: _forged("none"),
    lambda: _forged("HS256", b"anything"),                                    # algorithm-confusion attempt
    lambda: token(key=OTHER),                                                 # signed by someone else's key
    lambda: token(exp=int(time.time()) - 600),                                # expired
    lambda: token(nbf=int(time.time()) + 600),                                # not valid yet
    lambda: token(aud="api://00000000-0000-0000-0000-000000000000"),          # meant for another API
    lambda: token(iss="https://login.microsoftonline.com/99999999-0000-0000-0000-000000000000/v2.0"),   # another tenant
    lambda: token(iss="https://sts.windows.net/" + TENANT + "/"),             # v1 issuer
    lambda: token(tid="99999999-0000-0000-0000-000000000000"),
    lambda: token(ver="1.0"),
    lambda: token(oid=None, sub=None),                                        # no subject at all
    lambda: token()[:-4] + "AAAA",                                            # tampered signature
    lambda: "not.a.jwt",
])
def test_bad_tokens_are_refused_without_saying_why(bad):
    r = client.get("/me", headers=bearer(bad()))
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert r.json()["detail"] in ("Invalid or expired token.", "Invalid or missing credentials.")


def test_unknown_signing_key_is_a_401_but_unreachable_keys_are_a_503(monkeypatch):
    def unknown(_t):
        raise PyJWKClientError("Unable to find a signing key that matches")

    def down(_t):
        raise PyJWKClientConnectionError("boom")
    for provider, status in ((unknown, 401), (down, 503)):
        v = entra.EntraTokenValidator(tenant_id=TENANT, api_client_id=API_ID, signing_key_provider=provider)
        monkeypatch.setattr(entra, "get_validator", lambda v=v: v)
        assert client.get("/me", headers=bearer(token())).status_code == status


def test_malformed_authorization_headers_are_refused():
    for h in ("Basic abc", "Bearer", "Bearer   ", "token abc"):
        assert client.get("/me", headers={"Authorization": h}).status_code == 401


# ------------------------------------------------------------------ AUTH_MODE decides what is accepted
def test_entra_mode_refuses_api_keys_even_valid_ones():
    r = client.get("/me", headers={"X-API-Key": "k-alice"})
    assert r.status_code == 401 and "disabled" in r.json()["detail"]


def test_api_key_mode_refuses_bearer_tokens(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "api_key")
    assert client.get("/me", headers=bearer(token())).status_code == 401
    assert client.get("/me", headers={"X-API-Key": "k-alice"}).json()["auth"] == "api_key"


def test_hybrid_accepts_both_and_a_bad_bearer_never_falls_back_to_a_good_key(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "hybrid")
    assert client.get("/me", headers=bearer(token())).json()["auth"] == "entra_user"
    assert client.get("/me", headers={"X-API-Key": "k-alice"}).json()["auth"] == "api_key"
    mixed = {**bearer(token(key=OTHER)), "X-API-Key": "k-alice"}
    assert client.get("/me", headers=mixed).status_code == 401


def test_missing_credentials_get_a_challenge():
    r = client.get("/me")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


# ------------------------------------------------------------------ the whole workflow, signed in with Entra
GOOD = "Client ID: C10482\nEffective date: 2026-10-12\nEmail: client@example.com"


def test_automation_submits_and_a_person_approves_with_both_names_in_the_audit_trail():
    sub = client.post("/documents/d1/process", json={"document_type": "client_update", "event_id": "e1", "text": GOOD},
                      headers=bearer(app_token()))
    assert sub.status_code == 200
    aid = sub.json()["approval_id"]
    # a person cannot submit as automation, and automation cannot approve
    assert client.post("/documents/d2/process", json={"document_type": "client_update", "event_id": "e2", "text": GOOD},
                       headers=bearer(token())).status_code == 403
    assert client.post(f"/approvals/{aid}/decision", json={"decision": "approve", "note": "ok"},
                       headers=bearer(app_token())).status_code == 403
    ok = client.post(f"/approvals/{aid}/decision", json={"decision": "approve", "note": "checked the form"},
                     headers=bearer(token()))
    assert ok.status_code == 200
    events = client.get("/audit/events?limit=50", headers=bearer(token())).json()["items"]
    actors = {e["actor"] for e in events}
    assert {f"app:{AUTOMATION_APP}", "alice@contoso.com"} <= actors
    assert client.get("/audit/verify", headers=bearer(token())).json()["ok"] is True


# ------------------------------------------------------------------ configuration
def test_startup_refuses_entra_mode_without_tenant_or_with_service_groups(monkeypatch):
    monkeypatch.setattr(settings, "entra_tenant_id", "")
    with pytest.raises(RuntimeError):
        validate_auth_config()
    monkeypatch.setattr(settings, "entra_tenant_id", TENANT)
    monkeypatch.setattr(settings, "entra_api_client_id", "not-a-guid")
    with pytest.raises(RuntimeError):
        validate_auth_config()
    monkeypatch.setattr(settings, "entra_api_client_id", API_ID)
    monkeypatch.setattr(settings, "entra_group_roles", f"{GROUP}:service")
    with pytest.raises(RuntimeError, match="reviewer"):
        validate_auth_config()
    monkeypatch.setattr(settings, "entra_group_roles", "")
    validate_auth_config()


def test_production_starts_in_entra_mode_with_no_api_keys_at_all(monkeypatch):
    import asyncio

    from org_automation.main import lifespan

    async def start():
        async with lifespan(app):
            pass
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_keys", "")
    asyncio.run(start())                                      # Entra only: no keys needed
    monkeypatch.setattr(settings, "auth_mode", "hybrid")
    with pytest.raises(RuntimeError):
        asyncio.run(start())                                  # hybrid still needs real keys to be configured


def test_ui_config_exposes_only_public_sign_in_identifiers(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    cfg = client.get("/ui/config")
    body = cfg.json()["auth"]
    assert body["mode"] == "entra" and body["entra"] == {
        "tenant_id": TENANT, "client_id": SPA_ID, "authority": f"https://login.microsoftonline.com/{TENANT}",
        "api_scope": f"api://{API_ID}/access_as_user"}
    assert cfg.json()["demo_logins"] == []


def test_demo_logins_disappear_when_api_keys_are_off(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "api_keys", "flow:demo-service-key:service")
    assert client.get("/ui/config").json()["demo_logins"] == []
    monkeypatch.setattr(settings, "auth_mode", "hybrid")
    assert [p["name"] for p in client.get("/ui/config").json()["demo_logins"]] == ["flow"]


def test_page_csp_allows_microsoft_login_only_when_entra_is_on(monkeypatch):
    assert "https://login.microsoftonline.com" in client.get("/ui/").headers["content-security-policy"]
    monkeypatch.setattr(settings, "auth_mode", "api_key")
    assert "login.microsoftonline.com" not in client.get("/ui/").headers["content-security-policy"]
