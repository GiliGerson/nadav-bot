"""Typed configuration loaded from environment variables / .env."""
import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    anthropic_api_key: str

    claude_model: str = "claude-sonnet-5-5"
    bot_language: str = "en"
    cache_ttl_seconds: int = 300
    user_cooldown_seconds: int = 10
    log_level: str = "INFO"
    watchlist_db_path: str = "data/watchlist.db"

    webhook_base_url: str | None = None
    webhook_secret: str = "change-me"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
