"""PLAIN ENGLISH: The "on" switch. Creates the web app, plugs in the endpoints, and prepares the database.

Safety check at startup: if ENVIRONMENT=production but the demo passwords are still in use, the
app refuses to start rather than run insecurely.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from org_automation.api.routes import router
from org_automation.api.ui import ui_router
from org_automation.core.auth import demo_logins, parse_keys, uses_api_keys, validate_auth_config
from org_automation.core.settings import settings
from org_automation.storage.database import ensure_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level)
    validate_auth_config()
    if settings.environment == "production":
        if demo_logins() or (settings.docusign_hmac_secret
                             and settings.docusign_hmac_secret.get_secret_value().startswith("demo-")):
            raise RuntimeError("Refusing to start in production with demo credentials.")
        if uses_api_keys() and not parse_keys(settings.api_keys):
            raise RuntimeError("Refusing to start in production with no API keys configured.")
        if settings.auth_mode == "api_key":
            logging.getLogger("org_automation").warning(
                "AUTH_MODE=api_key: shared keys are demo-grade. Use AUTH_MODE=entra for production sign-in.")
    ensure_db()
    logging.getLogger("org_automation").warning(
        "CRM writes: %s (CRM_PROVIDER=%s). %s", settings.effective_write_mode().upper(), settings.crm_provider,
        "Approved changes are recorded but NOT written." if settings.effective_write_mode() == "shadow" else "Approved changes are written.")
    yield


app = FastAPI(title="Org Automation Scaffold", version="0.9.0", lifespan=lifespan)
app.include_router(router)
app.include_router(ui_router)

# The demo web screen: plain files in ./ui, no build step. It talks to the same API as everything else.
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "ui", html=True), name="ui")

# Strict page rules: only our own scripts and styles may run, nothing can frame the page, and browsers must not
# guess file types. (The screen also never inserts server text as HTML, so a hostile document can't run code.)
def _ui_csp() -> str:
    # Microsoft sign-in needs the page to call Microsoft's login host for tokens; allowed only when Entra is switched on.
    connect = "'self' https://login.microsoftonline.com" if settings.auth_mode != "api_key" else "'self'"
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src {connect}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/ui"):
        response.headers["Content-Security-Policy"] = _ui_csp()
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/ui/")
