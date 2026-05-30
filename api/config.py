"""Environment-backed settings. Secrets live in .env; never hardcode (CLAUDE.md §6)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    fflogs_client_id: str = Field(default="", alias="FFLOGS_CLIENT_ID")
    fflogs_client_secret: str = Field(default="", alias="FFLOGS_CLIENT_SECRET")
    database_url: str = Field(default="", alias="DATABASE_URL")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    web_origin: str = Field(default="http://localhost:5173", alias="WEB_ORIGIN")

    # Single-origin prod mode: when set, FastAPI serves the React build from
    # this directory at `/`. Leave empty in dev (Vite dev server handles it).
    web_static_dir: str = Field(default="", alias="WEB_STATIC_DIR")

    # HTTP Basic auth — gated on these being non-empty. Used for quick-tunnel
    # deployments where Cloudflare Access isn't available. Unset both when
    # upgrading to a named tunnel + Cloudflare Access (Access fronts auth).
    auth_username: str = Field(default="", alias="AUTH_USERNAME")
    auth_password: str = Field(default="", alias="AUTH_PASSWORD")

    # v1.7.1: optional developer password. When set, logging in with this
    # password marks the user as `is_developer=True` and unlocks dev-only
    # UI surfaces (Abilities review queue, Field data, etc.). When unset,
    # backwards-compat falls back to "username == AUTH_USERNAME → dev mode"
    # so a fresh install with one AUTH_USERNAME keeps the existing behavior.
    dev_password: str = Field(default="", alias="DEV_PASSWORD")

    # FFLogs user-OAuth redirect URI. Must match what's registered on the
    # FFLogs API client config page. Default assumes prod-runner port 8800.
    fflogs_redirect_uri: str = Field(
        default="http://127.0.0.1:8800/auth/fflogs/callback",
        alias="FFLOGS_REDIRECT_URI",
    )


settings = Settings()
