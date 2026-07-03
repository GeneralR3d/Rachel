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

    # OpenRouter
    openrouter_api_key: str
    # Separate key used for everything Graphiti-related (world-view LLM,
    # embedder, reranker). Falls back to openrouter_api_key when unset so
    # existing single-key setups keep working; see graphiti_api_key.
    openrouter_graphiti_api_key: str | None = None
    openrouter_model: str = "deepseek/deepseek-v4-flash"
    # OpenAI-compatible base URL for OpenRouter, shared by Graphiti's LLM /
    # embedder / reranker clients (OpenRouter now exposes /v1/embeddings).
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Smaller/cheaper model Graphiti uses for its internal helper + reranker calls.
    openrouter_small_model: str = "deepseek/deepseek-v4-flash"
    # Embedding model id (routed through OpenRouter) used by Graphiti's embedder.
    openrouter_embedding_model: str = "openai/text-embedding-3-small"

    # Neo4j connection for Graphiti (the world-view knowledge graph). The app
    # container reaches the service as `db`-style host `neo4j`; locally it's the
    # loopback-mapped port from docker-compose.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # Admin whitelist (your personal Telegram user id, from @userinfobot)
    admin_id: int = 0

    # Names used mainly for summarisation / history labelling
    bot_name: str = "Rachel"
    user_name: str | None = None

    # Async SQLAlchemy connection string (asyncpg driver)
    database_url: str = "postgresql+asyncpg://rachel:rachel@localhost:5432/rachel"

    # Markdown file holding Rachel's persistent "world view" (learned facts)
    worldview_path: str = "worldview.md"

    @property
    def graphiti_api_key(self) -> str:
        """OpenRouter key for Graphiti, falling back to the main key when unset."""
        return self.openrouter_graphiti_api_key or self.openrouter_api_key


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor so the .env is parsed only once."""
    return Settings()  # type: ignore[call-arg]
