"""The event contract.

This module is the single definition of what an inference event *is*. The
instrumentation wrapper produces it, the event bus transports it, the ingestion
worker validates against it, and the repository persists it. Producer and
consumer physically cannot drift, because there is only one schema and it lives
below all of them in the dependency graph.

Compatibility rule for changes: adding an optional field is safe and needs no
version bump. Removing a field, renaming one, or narrowing a type is breaking
-- bump `SCHEMA_VERSION`, keep the old shape parseable, and let the replay path
branch on the version recorded with each row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import InferenceStatus

#: Bump only on a breaking change. Persisted per row so old payloads stay
#: interpretable after the shape moves on.
SCHEMA_VERSION: Final = 1

#: `Final` so the type checker narrows this to its literal type and can verify
#: it against the `event_type` field's `Literal` annotation.
EVENT_TYPE_INFERENCE: Final = "inference.completed"


class InferenceEvent(BaseModel):
    """A completed LLM call, however it ended.

    "Completed" means terminal, not successful: errors and cancellations
    produce exactly one of these too. That is the property the error and
    cancellation dashboards depend on -- a gap in this stream would read as
    "nothing happened" rather than "something went wrong".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    event_type: Literal["inference.completed"] = EVENT_TYPE_INFERENCE

    #: Minted by the wrapper *before* the provider call, and reused as the
    #: `inference_logs` primary key. Redelivery from the stream therefore
    #: collides on the PK and is discarded, which is what makes at-least-once
    #: delivery idempotent instead of duplicate-producing.
    id: uuid.UUID

    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    status: InferenceStatus
    streamed: bool = False

    started_at: datetime
    completed_at: datetime

    latency_ms: int | None = Field(default=None, ge=0)
    ttft_ms: int | None = Field(default=None, ge=0)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    #: Already truncated and redacted by the producer. Redaction happens before
    #: the event leaves the API process, so unredacted content never reaches
    #: Redis, the worker, or the database.
    input_preview: str | None = None
    output_preview: str | None = None

    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = None
    finish_reason: str | None = Field(default=None, max_length=64)

    raw_metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.status is InferenceStatus.ERROR and not self.error_message:
            raise ValueError("error status requires error_message")
        return self

    def to_row(self) -> dict[str, Any]:
        """Column values for `inference_logs`.

        `event_type` is envelope metadata, not a column -- it selects the
        handler, it is not part of the record.
        """
        return self.model_dump(exclude={"event_type"})
