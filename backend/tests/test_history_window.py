"""How much conversation is replayed to the provider.

Every turn resends the window, so an untrimmed conversation grows its own prompt
on each exchange. That is not merely wasteful: on Groq's free tier, which allows
8000 tokens per minute and counts the reserved `max_completion_tokens` against
that budget, a long chat reached

    413 - Request too large ... Limit 8000, Requested 8690

and every subsequent message in that conversation failed. The brief asks for
*short* conversational context; these pin what "short" means.

Trimming applies only to what is sent upstream. The stored transcript stays whole.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.db.models import Message
from app.domain.enums import MessageRole
from app.services.chat import recent_window


def message(content: str, seq: int = 0) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role=MessageRole.USER,
        content=content,
        seq=seq,
        created_at=datetime.now(UTC),
    )


def test_keeps_only_the_most_recent_messages() -> None:
    history = [message(f"m{i}", i) for i in range(10)]

    kept = recent_window(history, max_messages=4, max_chars=100_000)

    assert [m.content for m in kept] == ["m6", "m7", "m8", "m9"]


def test_character_budget_trims_further_than_the_message_cap() -> None:
    """A message cap is not a size bound: ten short messages and ten essays both
    pass a count check."""
    history = [message("x" * 400, i) for i in range(10)]

    kept = recent_window(history, max_messages=50, max_chars=1000)

    assert len(kept) == 2, "400 + 400 fits in 1000; a third would not"
    assert sum(len(m.content) for m in kept) <= 1000


def test_the_current_question_survives_even_if_it_alone_blows_the_budget() -> None:
    """Dropping the newest message to satisfy a budget answers the wrong question.

    A user who pastes a large document and asks about it must still get an answer
    about *that document*, not about whatever preceded it.
    """
    history = [message("older", 0), message("y" * 5000, 1)]

    kept = recent_window(history, max_messages=50, max_chars=1000)

    assert len(kept) == 1
    assert kept[0].content.startswith("y")


def test_order_is_preserved_oldest_first() -> None:
    """The provider is given a conversation, not a reversed one."""
    history = [message(f"m{i}", i) for i in range(6)]
    kept = recent_window(history, max_messages=3, max_chars=100_000)

    assert [m.content for m in kept] == ["m3", "m4", "m5"]


def test_short_conversations_are_untouched() -> None:
    history = [message("hello", 0), message("hi", 1)]

    assert recent_window(history, max_messages=20, max_chars=100_000) == history
