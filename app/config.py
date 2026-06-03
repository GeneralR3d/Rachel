"""Application settings, loaded from environment variables / a local .env file.

Replaces the scattered ``os.environ[...]`` reads in the original app.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram app credentials (identify the client app, from my.telegram.org)
    telegram_api_id: int
    telegram_api_hash: str

    # Admin bot token (the second bot, from @BotFather)
    telegram_bot_token: str

    # Google Gemini
    gemini_api_key: str

    # Admin whitelist (your personal Telegram user id, from @userinfobot)
    admin_id: int = 0

    # Names used mainly for summarisation / history labelling
    bot_name: str = "Rachel"
    user_name: str | None = None

    # Async SQLAlchemy connection string (asyncpg driver)
    database_url: str = "postgresql+asyncpg://rachel:rachel@localhost:5432/rachel"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor so the .env is parsed only once."""
    return Settings()  # type: ignore[call-arg]
