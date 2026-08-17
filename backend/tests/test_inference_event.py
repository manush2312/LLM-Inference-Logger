"""Event contract invariants -- pure validation, no services needed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.enums import InferenceStatus
from app.domain.events import SCHEMA_VERSION, InferenceEvent

NOW = datetime.now(UTC)


def build(**overrides: object) -> InferenceEvent:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "provider": "mock",
        "model": "mock-1",
        "status": InferenceStatus.SUCCESS,
        "started_at": NOW,
        "completed_at": NOW + timedelta(milliseconds=50),
    }
    return InferenceEvent(**(defaults | overrides))  # type: ignore[arg-type]


def test_defaults_stamp_the_current_schema_version() -> None:
    event = build()
    assert event.schema_version == SCHEMA_VERSION
    assert event.event_type == "inference.completed"


def test_completion_may_not_precede_start() -> None:
    with pytest.raises(ValidationError, match="completed_at must not precede started_at"):
        build(completed_at=NOW - timedelta(seconds=1))


def test_error_status_requires_a_message() -> None:
    """An error row with no explanation is worse than no row at all."""
    with pytest.raises(ValidationError, match="error status requires error_message"):
        build(status=InferenceStatus.ERROR)


def test_negative_durations_are_rejected() -> None:
    with pytest.raises(ValidationError):
        build(latency_ms=-1)


def test_unknown_fields_are_rejected() -> None:
    """`extra="forbid"` turns a producer/consumer drift into a loud failure.

    Silently dropping an unrecognised field would let a renamed producer field
    vanish into the void and show up as a column of NULLs weeks later.
    """
    with pytest.raises(ValidationError):
        build(latency_seconds=1.2)


def test_round_trips_through_json_unchanged() -> None:
    """The bus transports JSON; the contract must survive the trip exactly."""
    original = build(
        conversation_id=uuid.uuid4(),
        ttft_ms=42,
        input_tokens=10,
        output_tokens=20,
        raw_metadata={"provider_request_id": "req_123"},
    )

    restored = InferenceEvent.model_validate_json(original.model_dump_json())

    assert restored == original


def test_to_row_drops_envelope_metadata_only() -> None:
    row = build().to_row()

    assert "event_type" not in row
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["provider"] == "mock"
