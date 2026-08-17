"""PII redaction for persisted previews.

Applied **only** to what gets stored (`input_preview` / `output_preview`).
Never to what is sent to the provider, and never to what the user sees live --
redacting those would corrupt the product to protect the logs.

Redaction runs in the API process, before the event is published, so
unredacted content never reaches Redis, the worker, or the database. Redacting
in the worker instead would leave raw PII sitting in the stream and in
`events_raw`, which is exactly where an audit would look for it.

**Known limitation, stated plainly:** these are regexes. They catch structured
identifiers -- emails, phone numbers, card-shaped digit runs, SSN-shaped
strings, API keys. They do not catch a name or a street address written in
prose, because that needs named-entity recognition. Microsoft Presidio is the
production upgrade path; this is the honest 80% and the README says so rather
than implying completeness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    placeholder: str


def _rule(name: str, pattern: str, placeholder: str) -> RedactionRule:
    return RedactionRule(name, re.compile(pattern, re.IGNORECASE), placeholder)


#: Order matters. Longer, more specific patterns run first so a card number is
#: not partially consumed by the phone-number rule before it is recognised.
DEFAULT_RULES: Final[tuple[RedactionRule, ...]] = (
    # Provider keys, before anything else -- leaking one of these into a log
    # is materially worse than leaking a phone number.
    _rule("api_key", r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b", "[REDACTED_API_KEY]"),
    _rule("bearer_token", r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", "[REDACTED_TOKEN]"),
    _rule("email", r"\b[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]"),
    _rule("iban", r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", "[REDACTED_IBAN]"),
    # 13-19 digits in groups, the shape of a payment card. Not Luhn-checked:
    # a false positive costs a redacted number in a log preview, a false
    # negative costs a real card number in the database.
    _rule("credit_card", r"\b(?:\d[ \-]?){13,19}\b", "[REDACTED_CARD]"),
    _rule("ssn", r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]"),
    _rule(
        "phone",
        r"(?:\+\d{1,3}[ \-.]?)?(?:\(\d{2,4}\)[ \-.]?|\d{2,4}[ \-.])\d{3,4}[ \-.]?\d{3,4}\b",
        "[REDACTED_PHONE]",
    ),
    _rule("ipv4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[REDACTED_IP]"),
)


class Redactor:
    """Truncate, then redact, then store."""

    def __init__(
        self,
        *,
        max_chars: int,
        enabled: bool = True,
        rules: tuple[RedactionRule, ...] = DEFAULT_RULES,
    ) -> None:
        self._max_chars = max_chars
        self._enabled = enabled
        self._rules = rules

    def preview(self, text: str | None) -> str | None:
        """Produce a stored preview: truncated first, then redacted.

        Truncating first bounds the work done on adversarially long input.
        Redacting first would mean running every pattern over a megabyte of
        text to then discard almost all of it.
        """
        if text is None:
            return None

        clipped = text[: self._max_chars]
        if len(text) > self._max_chars:
            clipped += "…"

        return self.redact(clipped)

    def redact(self, text: str) -> str:
        if not self._enabled:
            return text

        for rule in self._rules:
            text = rule.pattern.sub(rule.placeholder, text)
        return text
