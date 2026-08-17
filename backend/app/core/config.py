"""Application configuration.

One `Settings` object, loaded once, is the single source of truth for every
tunable in the system. Nothing else reads `os.environ` -- that keeps the
configuration surface auditable and makes tests able to override cleanly.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root, so a single .env at the top level serves backend + tooling.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class AppEnv(StrEnum):
    LOCAL = "local"
    DOCKER = "docker"
    PRODUCTION = "production"


class EventBusBackend(StrEnum):
    """Which transport carries inference events from the API to the ingestor.

    `MEMORY` exists so the API and its tests can run the full instrumentation
    path with no Redis. It is a real implementation of the same interface, not
    a stub -- see `app.events.bus`.
    """

    REDIS = "redis"
    MEMORY = "memory"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- App ---------------------------------------------------------------
    app_env: AppEnv = AppEnv.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- Postgres ----------------------------------------------------------
    postgres_user: str = "llm"
    postgres_password: str = "llm"
    postgres_db: str = "llm_logger"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- Redis -------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379

    # --- Event bus ---------------------------------------------------------
    event_bus_backend: EventBusBackend = EventBusBackend.REDIS
    event_stream_name: str = "inference_logs"
    event_stream_dlq: str = "inference_logs:dlq"
    event_consumer_group: str = "ingestors"

    #: Hard ceiling on how long the *request path* may spend publishing a log
    #: event. Exceeding it drops the event rather than delaying the user.
    event_publish_timeout_ms: int = Field(default=200, ge=1)

    #: Approximate cap on retained stream entries, so a stalled worker degrades
    #: into data loss rather than an out-of-memory Redis.
    event_stream_maxlen: int = Field(default=100_000, ge=1000)

    # --- Providers ---------------------------------------------------------
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    default_provider: str = "mock"
    default_anthropic_model: str = "claude-sonnet-5"
    default_openai_model: str = "gpt-4o"

    # --- Instrumentation ---------------------------------------------------
    preview_max_chars: int = Field(default=500, ge=0)
    redaction_enabled: bool = True

    # --- Derived -----------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async DSN used by the application at runtime."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        """Sync DSN, used only by Alembic which has no async story worth the cost."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def event_publish_timeout_s(self) -> float:
        return self.event_publish_timeout_ms / 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Use as a FastAPI dependency or call directly."""
    return Settings()
