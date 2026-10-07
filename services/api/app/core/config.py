"""Runtime configuration. Provider settings and secrets come from the environment only."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"

    database_url: str = "postgresql+psycopg://prod_app:prod_app_dev@localhost:5433/production"
    database_owner_url: str = "postgresql+psycopg://prod_owner:prod_owner_dev@localhost:5433/production"
    redis_url: str = "redis://localhost:6380/0"

    storage_endpoint_url: str | None = "http://localhost:9000"
    storage_bucket: str = "prodauto-local"
    storage_access_key_id: str | None = None
    storage_secret_access_key: str | None = None
    storage_region: str = "us-east-1"
    signed_url_ttl_seconds: int = Field(default=300, ge=30, le=300)
    # Browser origins allowed to PUT to signed upload URLs (applied to the bucket's CORS rules).
    storage_cors_origins: list[str] = ["http://localhost:3000"]

    malware_scanner: Literal["clamav", "disabled"] = "clamav"
    clamd_host: str = "localhost"
    clamd_port: int = 3310

    ocr_provider: Literal["none", "azure", "claude", "gemini"] = "none"
    azure_di_endpoint: str | None = None
    azure_di_key: str | None = Field(default=None, repr=False)
    azure_di_api_version: str = "2024-11-30"
    # OCR_PROVIDER=gemini: Google Gemini transcribes photos (a free AI Studio key works). Secret: never logged.
    gemini_api_key: SecretStr | None = Field(default=None, repr=False)
    gemini_model: str = "gemini-3.8-flash"
    # Tried in order when GEMINI_MODEL is busy (503 / 429) or retired (404).
    gemini_fallback_models: str = "gemini-3.5-flash,gemini-3.1-flash-lite"

    # AI extraction (decision D6: Anthropic Claude). The API key is read by the SDK from
    # ANTHROPIC_API_KEY and never stored in settings or logs. "none" = deterministic extractors only.
    ai_provider: Literal["none", "claude"] = "none"
    anthropic_model: str = "claude-opus-5-5"
    ai_timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    # Report summary (FR17): "template" = deterministic sentences only; "claude" = checked paraphrase with fallback.
    narrative_provider: Literal["template", "claude"] = "template"

    # Operations (M8). SENDS_PAUSED holds every outbound email (reports and reminders) in the queue, e.g. during an
    # incident or a restore drill; nothing is dropped. METRICS_TOKEN enables GET /api/v1/ops/metrics for scrapers.
    sends_paused: bool = False
    # Email channel. "graph": the worker sends through the connected Microsoft 365 mailbox (server side).
    # "emailjs": the Sender's browser sends through EmailJS (@emailjs/browser); the server still records the
    # confirmed intent, builds the template variables from the latest saved report and records the outcome.
    email_provider: Literal["graph", "emailjs"] = "graph"
    metrics_token: str | None = Field(default=None, repr=False)
    # EmailJS account for server-side sends (order PDFs, owner reports), read by the API and the worker only.
    # When set, these take precedence over the values an administrator stores in Settings -> Owner report & email.
    # The private key is a SecretStr: never printed in logs, errors or API responses, never sent to browsers.
    emailjs_service_id: str | None = None
    emailjs_template_id: str | None = None
    emailjs_public_key: str | None = None
    emailjs_private_key: SecretStr | None = Field(default=None, repr=False)

    # Integration credentials are encrypted with these keys ("id:base64key,..."; first encrypts). Secret store only.
    integration_keys: str | None = Field(default=None, repr=False)
    integration_timeout_seconds: float = Field(default=30.0, ge=5, le=120)

    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = "openid profile email"
    public_base_url: str = "http://localhost:8000"

    session_secret: str = Field(default="", repr=False)
    session_cookie_name: str = "pa_session"
    session_cookie_secure: bool = True
    session_ttl_hours: int = Field(default=12, ge=1, le=72)

    dev_auth_enabled: bool = False

    idempotency_ttl_hours: int = 24

    @model_validator(mode="after")
    def _guard(self) -> "Settings":
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        if self.dev_auth_enabled and self.app_env not in ("development", "test"):
            raise ValueError("DEV_AUTH_ENABLED is only permitted in development or test")
        if self.malware_scanner == "disabled" and self.app_env not in ("development", "test"):
            raise ValueError("MALWARE_SCANNER=disabled is only permitted in development or test")
        if self.ocr_provider == "azure" and not (self.azure_di_endpoint and self.azure_di_key):
            raise ValueError("OCR_PROVIDER=azure requires AZURE_DI_ENDPOINT and AZURE_DI_KEY")
        if self.ocr_provider == "gemini" and not (self.gemini_api_key and self.gemini_api_key.get_secret_value()):
            raise ValueError("OCR_PROVIDER=gemini requires GEMINI_API_KEY")
        if self.app_env in ("staging", "production") and not self.integration_keys:
            raise ValueError("INTEGRATION_KEYS is required outside development/test")
        if self.app_env in ("staging", "production") and not self.session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE must be true outside development/test")
        return self

    @property
    def dev_auth_active(self) -> bool:
        return self.dev_auth_enabled and self.app_env in ("development", "test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
