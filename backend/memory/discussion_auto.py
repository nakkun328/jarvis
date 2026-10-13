"""Owner-approved automatic memory from a finished group discussion. Off by default.

When a discussion ends with at least one persona turn and a moderator summary, this module may
stage ONE memory candidate from that summary (and optionally approve it through
``MemoryWriter`` with the fixed audit actor ``auto:discussion``). Properties, all covered by tests:

* The note is labelled as an AI discussion OPINION without factual backing. Importance and
  confidence are the lowest fixed constants below.
* Nothing is sent to a model. The summary and topic are checked deterministically with the same
  sensitivity and instruction-like filters as chat and research memory. A topic that carries a
  private marker is not stored at all; a summary or topic that trips the sensitive-data or
  instruction filters is staged ``pending`` and is never auto-approved.
* The id is derived from the conversation id, so one conversation is never staged twice. A
  per-day cap bounds how many notes are staged (in-memory counter, server local UTC day).
* ``observe`` never raises and logs a fixed code only, never the topic, the summary or an
  exception message. Origin ``discussion`` is plain text in SQLite (no CHECK constraint), so no
  schema change is needed (SCHEMA_VERSION stays 10).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.memory.auto_approval import DISCUSSION_AUTO_APPROVER
from backend.memory.events import (
    MemoryEventKind,
    MemoryEventOrigin,
    MemoryEventPublisher,
    safe_publish,
    summarize,
)
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.repository import (
    MemoryAlreadyExists,
    MemoryRepository,
    MemoryRepositoryError,
    StoredMemory,
)
from backend.memory.sensitivity import fold, has_private_marker, is_sensitive
from backend.memory.writer import MemoryWriteError, MemoryWriter
from backend.research.claim_safety import looks_like_instruction

_LOG = logging.getLogger(__name__)

DISCUSSION_TAG = "discussion"
SOURCE_PREFIX = "discussion:"
DEFAULT_PER_DAY_LIMIT = 10
MAX_PER_DAY_LIMIT = 100
#: Fixed first line of every discussion-derived note: an AI opinion, not a verified fact.
LABEL = "議論の結果(AIの意見・事実の裏付けなし)"
#: The lowest values used anywhere for automatic memory: nothing here is verified.
IMPORTANCE = 0.2
CONFIDENCE = 0.2
MAX_SUMMARY_CHARS = 2000
_NAMESPACE = uuid5(NAMESPACE_URL, "jarvis:discussion-memory")


class Skip(StrEnum):
    """Why nothing was staged. Fixed words; never carries text."""

    EMPTY = "empty"
    PRIVATE = "private"
    DAILY_LIMIT = "daily_limit"
    DUPLICATE = "duplicate"


class Failure(StrEnum):
    """Fixed codes logged when memory creation fails."""

    STORAGE = "storage"
    APPROVAL = "approval"
    INDEX = "index"
    INTERNAL = "internal"


@dataclass(frozen=True)
class Outcome:
    staged: StoredMemory | None = None
    approved: bool = False
    skipped: Skip | None = None
    failure: Failure | None = None


def memory_id(conversation_id: UUID) -> UUID:
    return uuid5(_NAMESPACE, str(conversation_id))


def render_content(topic: str, summary: str, conversation_id: UUID, day: date) -> str:
    title = summarize(topic)
    return (
        f"{LABEL}\n議題: {title}\n\n{summary.strip()[:MAX_SUMMARY_CHARS]}\n\n"
        f"日付: {day.isoformat()}\n議論ID: {conversation_id}"
    )


def _today() -> date:
    return datetime.now(UTC).date()


class DiscussionAutoMemory:
    def __init__(
        self,
        repository: MemoryRepository,
        writer: MemoryWriter | None = None,
        *,
        auto_approve: bool = False,
        per_day_limit: int = DEFAULT_PER_DAY_LIMIT,
        today: Callable[[], date] = _today,
        publisher: MemoryEventPublisher | None = None,
        index_refresher: Callable[[UUID], None] | None = None,
    ) -> None:
        """``index_refresher(memory_id)`` is the derived-index refresh run after an approval when
        vector search is enabled; it runs synchronously, so call ``observe`` from a worker
        thread. A refresh failure is logged with a fixed code and never undoes the approval."""
        if auto_approve and writer is None:
            raise ValueError("Automatic approval needs a memory writer")
        if (
            isinstance(per_day_limit, bool)
            or not isinstance(per_day_limit, int)
            or not 1 <= per_day_limit <= MAX_PER_DAY_LIMIT
        ):
            raise ValueError("per_day_limit is out of range")
        self._repository = repository
        self._writer = writer
        self._auto_approve = auto_approve
        self.per_day_limit = per_day_limit
        self._today = today
        self._publisher = publisher
        self._index_refresher = index_refresher
        self._day = today()
        self._staged_today = 0

    @staticmethod
    def _log(code: str) -> None:
        # Only a fixed code; never the topic, the summary or an exception message.
        _LOG.warning("discussion_memory.failed", extra={"code": code})

    def observe(self, conversation_id: UUID, topic: str, summary: str) -> Outcome:
        """Stage (and maybe approve) the memory of one finished discussion. Never raises."""
        try:
            return self._observe(conversation_id, topic, summary)
        except Exception:  # memory creation must never affect the discussion
            self._log(Failure.INTERNAL.value)
            return Outcome(failure=Failure.INTERNAL)

    def _observe(self, conversation_id: UUID, topic: str, summary: str) -> Outcome:
        if (
            not isinstance(topic, str)
            or not isinstance(summary, str)
            or not topic.strip()
            or not summary.strip()
        ):
            return Outcome(skipped=Skip.EMPTY)
        folded_topic, folded_summary = fold(topic), fold(summary)
        if has_private_marker(folded_topic):
            return Outcome(skipped=Skip.PRIVATE)
        day = self._today()
        if day != self._day:
            self._day, self._staged_today = day, 0
        if self._staged_today >= self.per_day_limit:
            return Outcome(skipped=Skip.DAILY_LIMIT)
        record = MemoryRecord(
            id=memory_id(conversation_id),
            category=MemoryCategory.CONVERSATION,
            content=render_content(topic, summary, conversation_id, day),
            source=f"{SOURCE_PREFIX}{conversation_id}",
            origin=MemoryOrigin.DISCUSSION,
            importance=IMPORTANCE,
            confidence=CONFIDENCE,
            tags=(DISCUSSION_TAG,),
        )
        safe = not (
            is_sensitive(folded_summary)
            or is_sensitive(folded_topic)
            or has_private_marker(folded_summary)
            or looks_like_instruction(topic, summary)
        )
        try:
            stored = self._repository.add(record)
        except MemoryAlreadyExists:
            return Outcome(skipped=Skip.DUPLICATE)
        except (MemoryRepositoryError, ValueError):
            self._log(Failure.STORAGE.value)
            return Outcome(failure=Failure.STORAGE)
        self._staged_today += 1
        kind = MemoryEventKind.STAGED
        approved = False
        failure = None
        if self._auto_approve and self._writer is not None and safe:
            try:
                stored = self._writer.approve(record.id, actor=DISCUSSION_AUTO_APPROVER)
                approved = True
                kind = MemoryEventKind.APPROVED
            except (MemoryWriteError, MemoryRepositoryError, ValueError, RuntimeError):
                # Stays pending, for a person to review.
                self._log(Failure.APPROVAL.value)
                failure = Failure.APPROVAL
        if approved and self._index_refresher is not None:
            try:
                self._index_refresher(record.id)
            except Exception:  # the approval stands; the derived index is rebuildable
                self._log(Failure.INDEX.value)
                failure = Failure.INDEX
        safe_publish(
            self._publisher,
            kind,
            MemoryEventOrigin.DISCUSSION,
            record.id,
            f"{LABEL}: {summarize(topic)}",
        )
        return Outcome(staged=stored, approved=approved, failure=failure)
