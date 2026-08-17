"""Redaction behaviour, including the limits it does not claim to cover."""

from __future__ import annotations

import pytest

from app.instrumentation.redaction import Redactor


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(max_chars=500)


@pytest.mark.parametrize(
    ("text", "must_not_contain", "placeholder"),
    [
        ("write to manush@example.com", "manush@example.com", "[REDACTED_EMAIL]"),
        ("card 4111 1111 1111 1111 please", "4111", "[REDACTED_CARD]"),
        ("ssn 123-45-6789", "123-45-6789", "[REDACTED_SSN]"),
        ("call +1 415 555 0199", "555 0199", "[REDACTED_PHONE]"),
        ("host is 192.168.1.24", "192.168.1.24", "[REDACTED_IP]"),
        ("key sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghij", "[REDACTED_API_KEY]"),
        ("Authorization: Bearer abcdefghijklmnopqrst", "abcdefghijklmnop", "[REDACTED_TOKEN]"),
    ],
)
def test_structured_identifiers_are_removed(
    redactor: Redactor, text: str, must_not_contain: str, placeholder: str
) -> None:
    result = redactor.redact(text)

    assert must_not_contain not in result
    assert placeholder in result


def test_api_keys_are_redacted_before_anything_else(redactor: Redactor) -> None:
    """Leaking a provider key into a log is worse than leaking a phone number."""
    result = redactor.redact("use sk-livekey1234567890abcdefgh to authenticate")

    assert "sk-livekey" not in result
    assert "[REDACTED_API_KEY]" in result


def test_ordinary_text_is_untouched(redactor: Redactor) -> None:
    """Over-redaction destroys the previews' usefulness."""
    text = "How do I tune autovacuum on a write-heavy Postgres table?"

    assert redactor.redact(text) == text


def test_previews_are_truncated_and_marked(redactor: Redactor) -> None:
    result = redactor.preview("x" * 900)

    assert result is not None
    assert len(result) == 501, "500 characters plus the ellipsis"
    assert result.endswith("…")


def test_short_text_is_not_marked_as_truncated(redactor: Redactor) -> None:
    assert redactor.preview("short") == "short"


def test_absent_text_stays_absent(redactor: Redactor) -> None:
    """None means "no content", which is different from an empty preview."""
    assert redactor.preview(None) is None


def test_truncation_happens_before_matching() -> None:
    """Bounds the work done on adversarially long input.

    Redacting first would run every pattern across the whole string only to
    discard almost all of it.
    """
    redactor = Redactor(max_chars=10)

    result = redactor.preview("0123456789 manush@example.com")

    assert result == "0123456789…"


def test_redaction_can_be_disabled_for_local_debugging() -> None:
    disabled = Redactor(max_chars=500, enabled=False)

    assert disabled.redact("manush@example.com") == "manush@example.com"


def test_freeform_pii_is_not_caught() -> None:
    """Documents the known gap rather than implying completeness.

    Names and prose addresses need named-entity recognition; this asserts the
    limitation so the README's claim stays honest and a future NER upgrade has
    a failing test to flip.
    """
    redactor = Redactor(max_chars=500)
    text = "My name is Manush Darji and I live at 12 Rosewood Lane."

    assert redactor.redact(text) == text
