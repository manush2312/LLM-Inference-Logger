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
from urllib.parse import quote

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.providers.openai_compatible import (
    GEMINI_BASE_URL,
    GROQ_BASE_URL,
    OLLAMA_BASE_URL,
)

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

    #: Unset for local work, where Redis is only reachable inside the compose
    #: network. Required in any deployment where the host has a public address:
    #: an unauthenticated Redis on a routable IP is one of the most reliably
    #: exploited things on the internet, and network isolation alone is a single
    #: firewall mistake away from being the only thing protecting it.
    redis_password: str | None = None

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
    default_anthropic_model: str = "claude-opus-5"
    default_openai_model: str = "gpt-4o"

    # --- OpenAI-compatible backends ----------------------------------------
    # Groq, Gemini and Ollama all speak OpenAI's wire format at a different
    # address, so each needs only a credential, a URL and a default model.

    # Vendor catalogues churn faster than this file does. Both of these
    # defaults were wrong on first contact -- `llama-3.3-70b-versatile` and
    # `gemini-2.0-flash` had both been retired -- and each produced a clean 404
    # naming the problem, which is the adapters' error translation working. Ask
    # the vendor rather than trusting a default that has aged:
    #   GET {base_url}/models   with the same Authorization header.

    #: Groq. Free tier, no card required.
    groq_api_key: str | None = None
    groq_base_url: str = GROQ_BASE_URL
    default_groq_model: str = "openai/gpt-oss-20b"
    #: Below the free tier's 8000 tokens-per-minute budget, which
    #: `max_completion_tokens` counts against in full.
    groq_max_output_tokens: int = Field(default=4096, ge=256)

    #: Google Gemini via its OpenAI-compatibility endpoint. Free tier.
    #: `-latest` rather than a pinned version, precisely because pinning is what
    #: went stale here.
    gemini_api_key: str | None = None
    gemini_base_url: str = GEMINI_BASE_URL
    default_gemini_model: str = "gemini-flash-latest"

    #: A model on this machine. Gated on an explicit opt-in rather than probed:
    #: registering a provider we cannot reach would turn a clear "not
    #: configured" error into a connection failure mid-stream.
    ollama_enabled: bool = False
    #: Inside a container `localhost` is the container, so compose and
    #: Kubernetes must point at the host explicitly.
    ollama_base_url: str = OLLAMA_BASE_URL
    default_ollama_model: str = "llama3.2"

    #: Caps provider output. On models with reasoning enabled this budget
    #: covers reasoning *and* the visible answer, so a value tuned for answer
    #: length alone truncates mid-sentence.
    max_output_tokens: int = Field(default=16_000, ge=256)

    # --- Conversation context ----------------------------------------------
    #: How many past messages are replayed to the provider. Every turn resends the
    #: whole window, so an untrimmed conversation grows the prompt on every
    #: exchange -- cost and latency rise with it, and eventually the request is
    #: simply refused. Observed on Groq's free tier, which allows 8000 tokens per
    #: minute and counts the reserved `max_completion_tokens` against that budget:
    #: a long chat reached "Requested 8690, Limit 8000" and every further message
    #: failed. The brief asks for *short* conversational context, and this is what
    #: makes it short.
    max_history_messages: int = Field(default=20, ge=2)

    #: Second bound, because message count alone is not one: a single pasted essay
    #: can exceed the budget on its own. Characters rather than tokens to avoid a
    #: per-provider tokenizer dependency for a limit that only needs to be
    #: approximately right -- roughly 4 characters per token.
    #:
    #: 12000 chars is ~3000 tokens, and the number is derived from the tightest
    #: provider rather than picked. Groq's free tier allows 8000 tokens per minute
    #: and counts the *reserved* `groq_max_output_tokens` (4096) against it, not
    #: the tokens actually generated. So the usable prompt budget is 8000 - 4096 =
    #: 3904 tokens, and anything above that fails outright with a 413.
    #:
    #: A first attempt at 24000 was measured rather than assumed: the prompt
    #: plateaued at 4300 tokens, which plus 4096 is 8396 -- still over the limit.
    #: Trimming history rather than lowering the output reservation is the right
    #: trade here, because the requests most likely to hit the ceiling ("give me a
    #: roadmap for X") are exactly the ones that need a long answer.
    max_history_chars: int = Field(default=12_000, ge=1000)

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
    def redis_url(self) -> str:
        # quote() the password rather than interpolating it raw: a generated
        # secret routinely contains '@', '/' or ':', any of which silently
        # reshapes the URL and produces a connection failure that looks like a
        # wrong host rather than a quoting bug.
        auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/0"

    @property
    def event_publish_timeout_s(self) -> float:
        return self.event_publish_timeout_ms / 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Use as a FastAPI dependency or call directly."""
    return Settings()
