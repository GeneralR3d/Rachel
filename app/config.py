"""Application settings, loaded from environment variables / a local .env file.

Replaces the scattered ``os.environ[...]`` reads in the original app.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
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

    # --- Merge Gateway (OpenAI-compatible LLM/embedding endpoint) ---
    # Every LLM and embedding call goes through Merge Gateway. Its endpoint
    # speaks the OpenAI API, so both the LangChain clients (ChatOpenAI) and
    # Graphiti's OpenAI-SDK clients talk to it with nothing but a key +
    # base_url change.
    merge_gateway_api_key: str
    # The Gateway serves its OpenAI-compatible /chat/completions under the
    # /v1/openai path; plain /v1 is the Gateway-native API and is where
    # /embeddings lives. Chat clients use the first, the embedder the second.
    merge_gateway_base_url: str = "https://api-gateway.merge.dev/v1"
    merge_gateway_openai_base_url: str = "https://api-gateway.merge.dev/v1/openai"
    # Separate key used for everything Graphiti-related (world-view LLM,
    # embedder, reranker). Falls back to merge_gateway_api_key when unset so
    # single-key setups keep working; see graphiti_api_key.
    merge_gateway_graphiti_api_key: str | None = None

    # Replaced by the MERGE_GATEWAY_* settings above; kept for reference.
    # openrouter_api_key: str
    # openrouter_graphiti_api_key: str | None = None
    # openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Model ids, in the provider/model format the Gateway expects. Read from
    # LLM_* env vars, still accepting the old OPENROUTER_* names so existing
    # deployments don't need their .env rewritten.
    llm_model: str = Field(
        "deepseek/deepseek-v4-flash",
        validation_alias=AliasChoices("llm_model", "openrouter_model"),
    )
    # Smaller/cheaper model Graphiti uses for its internal helper + reranker calls.
    llm_small_model: str = Field(
        "deepseek/deepseek-v4-flash",
        validation_alias=AliasChoices("llm_small_model", "openrouter_small_model"),
    )
    # Embedding model id used by Graphiti's embedder.
    llm_embedding_model: str = Field(
        "openai/text-embedding-3-small",
        validation_alias=AliasChoices("llm_embedding_model", "openrouter_embedding_model"),
    )

    # Neo4j connection for Graphiti (the world-view knowledge graph). The app
    # container reaches the service as `db`-style host `neo4j`; locally it's the
    # loopback-mapped port from docker-compose.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # Admin whitelist (your personal Telegram user id, from @userinfobot)
    admin_id: int = 0

    # Names used mainly for summarisation / history labelling
    bot_name: str = "Bryan"
    user_name: str | None = None

    # Async SQLAlchemy connection string (asyncpg driver)
    database_url: str = "postgresql+asyncpg://rachel:rachel@localhost:5432/rachel"

    # Markdown file holding Bryan's persistent "world view" (learned facts)
    worldview_path: str = "worldview.md"

    @property
    def graphiti_api_key(self) -> str:
        """Gateway key for Graphiti, falling back to the main key when unset."""
        return self.merge_gateway_graphiti_api_key or self.merge_gateway_api_key


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor so the .env is parsed only once."""
    return Settings()  # type: ignore[call-arg]
