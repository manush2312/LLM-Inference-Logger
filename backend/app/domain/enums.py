"""Domain vocabulary shared by the database, the event contract and the API.

Defined once, here, so a value can never mean one thing in the wrapper and a
subtly different thing in the ingestion worker.
"""

from __future__ import annotations

from enum import StrEnum


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class InferenceStatus(StrEnum):
    """Terminal outcome of a single LLM call.

    Exactly one of these is recorded per call, including calls that failed or
    were abandoned -- that completeness is what makes the error and
    cancellation dashboards trustworthy rather than merely suggestive.
    """

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class ProcessingStatus(StrEnum):
    """Lifecycle of a raw event inside the ingestion pipeline."""

    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
