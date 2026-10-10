"""A small in-process feed of "a memory was just made" events for the Activity view.

Chat auto-memory and research staging run after the chat reply has finished, so the per-turn
activity stream has usually ended by then. They publish here instead, and the page polls
``GET /api/memory/activity`` (docs/memory.md, "Memory activity feed").

Properties, all covered by tests:

* Bounded: a ring buffer of the last ``MAX_EVENTS`` events. Nothing is persisted; a restart
  empties it and starts ``seq`` again at 1.
* Ordered: ``seq`` grows by one per event, never repeats and never goes backwards.
* An event is ``{seq, at, kind, origin, memory_id, summary}``. ``summary`` is the first line of
  the fact, stripped of control, format and bidi characters and cut to ``SUMMARY_CHARS``. It is
  the owner's own content, served only to the logged-in owner, and is never logged.
* Publishing never raises and never blocks: ``safe_publish`` swallows any failure and logs only
  a fixed code. Nothing here reads or writes memory itself.
"""

import logging
import threading
import unicodedata
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

_LOG = logging.getLogger(__name__)

MAX_EVENTS = 100
MAX_PAGE = 50
SUMMARY_CHARS = 80
FAILURE_CODE = "memory_event_publish_failed"


class MemoryEventKind(StrEnum):
    STAGED = "staged"
    APPROVED = "approved"
    WITHDRAWN = "withdrawn"


class MemoryEventOrigin(StrEnum):
    CHAT = "chat"
    RESEARCH = "research"


#: ``publisher(kind, origin, memory_id, content)``; ``content`` is the stored note text.
MemoryEventPublisher = Callable[[MemoryEventKind, MemoryEventOrigin, UUID, str], None]


def _unsafe(char: str) -> bool:
    # Control, format (zero-width and bidi), surrogate, private-use, unassigned, line/paragraph.
    return unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def summarize(content: str) -> str:
    """First line of ``content``, display-safe, at most ``SUMMARY_CHARS`` characters."""
    first = content.strip().split("\n", 1)[0] if isinstance(content, str) else ""
    kept = "".join(
        " " if char.isspace() else char for char in first if char.isspace() or not _unsafe(char)
    )
    text = " ".join(kept.split())
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


class MemoryActivityFeed:
    def __init__(self, max_events: int = MAX_EVENTS) -> None:
        if isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._seq = 0
        self._lock = threading.Lock()

    def publish(
        self, kind: MemoryEventKind, origin: MemoryEventOrigin, memory_id: UUID, content: str
    ) -> None:
        kind_value = MemoryEventKind(kind).value  # validate before a seq is spent
        origin_value = MemoryEventOrigin(origin).value
        summary = summarize(content)
        at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._seq += 1
            self._events.append(
                {
                    "seq": self._seq,
                    "at": at,
                    "kind": kind_value,
                    "origin": origin_value,
                    "memory_id": str(memory_id),
                    "summary": summary,
                }
            )

    @property
    def latest(self) -> int:
        with self._lock:
            return self._seq

    def since(self, after: int = 0, limit: int = MAX_PAGE) -> list[dict[str, Any]]:
        """Events with ``seq > after``, oldest first, at most ``limit`` of them."""
        with self._lock:
            found = [dict(event) for event in self._events if event["seq"] > after]
        return found[: max(0, min(limit, MAX_PAGE))]


def safe_publish(
    publisher: MemoryEventPublisher | None,
    kind: MemoryEventKind,
    origin: MemoryEventOrigin,
    memory_id: UUID,
    content: str,
) -> None:
    """Publish if a publisher is wired; swallow any failure (only a fixed code is logged)."""
    if publisher is None:
        return
    try:
        publisher(kind, origin, memory_id, content)
    except Exception:  # the feed must never affect chat, research or memory
        _LOG.warning("memory_events.failed", extra={"code": FAILURE_CODE})
