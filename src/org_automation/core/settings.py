"""PLAIN ENGLISH: The app's control panel.

Every adjustable knob (which database file, whether to use Claude, how picky to be about AI
confidence, who is allowed in) lives here. Values come from the private `.env` file or from
environment variables, so nothing sensitive is hard-coded in the code.
"""
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Org Automation Scaffold"
    environment: str = "development"
    database_path: str = "./org_dev.sqlite3"
    log_level: str = "INFO"

    # "demo" = rule-based reader, no AI. "claude" = real Claude via the Anthropic API.
    extraction_mode: Literal["demo", "claude"] = "demo"
    # SecretStr means the key is hidden if the settings object is ever printed or logged.
    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-sonnet-5-5"
    # Runtime-selectable generation provider. OpenAI-compatible includes local Ollama endpoints.
    llm_provider: Literal["claude", "openai-compatible"] = "claude"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "llama3.1"
    llm_api_key: SecretStr | None = None
    # Embeddings are independent from generation. "none" preserves the offline BM25 behavior.
    embedding_mode: Literal["none", "ollama"] = "none"
    embedding_base_url: str = "http://localhost:11434"
    embedding_model: str = "nomic-embed-text"
    min_confidence: float = 0.85

    # "scripted" = fixed, no-AI planner. "llm" = configured provider drives tool selection.
    # "claude" remains accepted as a legacy alias for existing .env files.
    agent_mode: Literal["scripted", "llm", "claude"] = "scripted"
    agent_max_tool_calls: int = 20   # hard budget per run, so a confused or hijacked agent can't loop forever

    # "name:secret:role,name:secret:role" — see .env.example. Empty = nobody can log in.
    # Which CRM the app talks to: "mock" (pretend, built in) or "salesforce" (see docs/SALESFORCE_SETUP.md).
    crm_provider: Literal["mock", "salesforce"] = "mock"
    salesforce_domain: str = ""                       # e.g. https://yourfirm.my.salesforce.com (My Domain URL)
    salesforce_client_id: str = ""                    # the External Client App's consumer key
    salesforce_client_secret: SecretStr | None = None
    salesforce_api_version: str = "v62.0"             # set to your org's current API version
    salesforce_object: str = "Contact"                # or "Account" (Person Accounts); see setup doc
    salesforce_client_id_field: str = "Client_ID__c"  # custom External ID field holding the firm's client number
    salesforce_email_field: str = "Email"
    salesforce_effective_date_field: str = "Email_Effective_Date__c"
    salesforce_timeout_seconds: float = 10.0

    # Microsoft 365 / Graph (not wired yet). Empty = disabled. Real values go in .env only, never in code.
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: SecretStr | None = None
    graph_site_id: str = ""                           # the ONE SharePoint site the app may touch (Sites.Selected)
    graph_folder: str = "Client Updates"              # folder (in that site's default library) where approval records go
    graph_timeout_seconds: float = 15.0
    # Where approval records are filed after a change is approved: "none" (default), "mock" (local demo), "sharepoint".
    # A real archive follows WRITE_MODE like the CRM does: it only writes when WRITE_MODE=enabled is set explicitly.
    archive_provider: Literal["none", "mock", "sharepoint"] = "none"

    # How callers prove who they are. "api_key" (default, demo-grade shared keys), "entra" (Microsoft Entra ID sign-in only;
    # API keys are refused), or "hybrid" (both, for migration). See core/entra.py and the README.
    auth_mode: Literal["api_key", "entra", "hybrid"] = "api_key"
    entra_tenant_id: str = ""                         # your directory (tenant) id
    entra_api_client_id: str = ""                     # app registration that represents THIS API (the token audience)
    entra_spa_client_id: str = ""                     # app registration for the browser page (public client, PKCE)
    entra_required_scope: str = "access_as_user"      # delegated scope a person's token must carry
    entra_group_roles: str = ""                       # optional "<group-object-id>:reviewer,..." (people only)

    # Whether approved changes may really be written to the CRM.
    #   "shadow"  = look, decide, log "would have written X" -- but NEVER write. (Misspell it and the app refuses to start.)
    #   "enabled" = real writes.
    # Not set at all: the pretend CRM writes (it is harmless); any REAL CRM stays in shadow. Real writes must be asked for.
    write_mode: Literal["shadow", "enabled"] | None = None

    # Optional: how many minutes one manual client update takes today (measure it, don't guess). Only used for the scorecard's
    # hours-covered estimate; unset means no estimate is shown.
    manual_minutes_per_update: float | None = None

    # Safe local-demo defaults make a fresh ZIP runnable without a private .env file.
    # main.py refuses to start in production while any demo key remains configured.
    api_keys: str = "flow:demo-service-key:service,alice:demo-alice-key:reviewer,bob:demo-bob-key:reviewer"
    docusign_hmac_secret: SecretStr | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def effective_write_mode(self) -> str:
        """The write mode actually in force. An explicit setting always wins; otherwise fail closed for real systems."""
        if self.write_mode:
            return self.write_mode
        return "enabled" if self.crm_provider == "mock" else "shadow"


settings = Settings()
