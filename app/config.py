"""Typed configuration loaded from environment variables / .env."""
import logging
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Placeholder secrets that are public (in this repo), so they must never guard a live webhook.
KNOWN_PLACEHOLDER_SECRETS = {"change-me", "a-long-random-string_only-letters-digits-_-"}
MIN_WEBHOOK_SECRET_LENGTH = 32
MIN_DASHBOARD_PASSWORD_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    telegram_bot_token: str
    anthropic_api_key: str

    claude_model: str = "claude-sonnet-5-5"
    bot_language: str = "en"
    cache_ttl_seconds: int = 300
    user_cooldown_seconds: int = 10
    user_requests_per_minute: int = 20  # every command, AI or not
    daily_ai_limit: int = 50
    # Telegram user IDs allowed to trigger Claude calls, e.g. "123,456". Empty = nobody.
    # NoDecode: read the env value as plain text instead of JSON, so commas work.
    ai_allowed_user_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    log_level: str = "INFO"
    # SQLite file shared by the watchlist and the daily AI usage counter.
    watchlist_db_path: str = "data/watchlist.db"

    # Render sets RENDER_EXTERNAL_URL to the service's public URL, so no manual step is needed there.
    webhook_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("WEBHOOK_BASE_URL", "RENDER_EXTERNAL_URL")
    )
    webhook_secret: str = "change-me"

    # HTTP Basic auth for the dashboard and JSON API. Optional locally (the server only
    # listens on localhost); required once deployed, see _require_production_secrets.
    dashboard_username: str = "nadav"
    dashboard_password: str = ""
    # Set when the server is reachable from the internet without a webhook (e.g. polling
    # inside a container bound to 0.0.0.0). The Dockerfile sets it, so images are safe by default.
    public_deployment: bool = False
    # Local polling refuses to start while a webhook is live (it would silently switch the
    # deployed bot off). Set this to take the bot back from the deployment on purpose.
    force_polling: bool = False

    @property
    def webhook_mode(self) -> bool:
        return bool(self.webhook_base_url)

    @property
    def production(self) -> bool:
        """Reachable from the internet, so the dashboard must have a real password.

        Not inferred from the webhook alone: a container running in polling mode is just
        as public, and the localhost Host check can't tell (the Host header is client-set).
        """
        return self.webhook_mode or self.public_deployment

    @field_validator("ai_allowed_user_ids", mode="before")
    @classmethod
    def _split_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            # Non-numeric entries fail validation at startup rather than silently locking users out.
            return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)
        return value

    @model_validator(mode="after")
    def _require_production_secrets(self) -> "Settings":
        if not self.production:
            return self
        generate = 'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
        # The secret header is the only proof a webhook request came from Telegram. With a
        # guessable one, anyone could post forged updates "from" an allowlisted user ID.
        if self.webhook_mode and (
            self.webhook_secret in KNOWN_PLACEHOLDER_SECRETS
            or len(self.webhook_secret) < MIN_WEBHOOK_SECRET_LENGTH
        ):
            raise ValueError(
                f"WEBHOOK_SECRET must be a random string of at least {MIN_WEBHOOK_SECRET_LENGTH} "
                f"characters when WEBHOOK_BASE_URL is set. {generate}"
            )
        # A public dashboard would expose the owner's watchlist and let anyone edit it.
        if len(self.dashboard_password) < MIN_DASHBOARD_PASSWORD_LENGTH:
            raise ValueError(
                f"DASHBOARD_PASSWORD must be at least {MIN_DASHBOARD_PASSWORD_LENGTH} characters "
                f"when WEBHOOK_BASE_URL or PUBLIC_DEPLOYMENT is set. {generate}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
