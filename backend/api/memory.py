"""Read-only memory endpoints: reviewed notes and pending candidates, with search and detail.

Everything here reports what the memory repository has persisted. Nothing is
approved, rejected, edited or retired from this API, and the Obsidian vault is
never read: only the SQLite review records are listed. The `content` of an
approved note is therefore the text recorded when it was reviewed. A person may
have edited the vault note since, so this is a review view and is not the text
the model receives (retrieval reads the current vault note).

Stored text (content, source, tags, project) is untrusted data and is only ever
placed inside JSON string values. Responses are built from explicit allowlists of
fields, so a column added to a record later is not exposed by accident. Error
bodies carry a fixed code and never repeat anything the caller sent or anything
that was stored.
"""

import logging
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from backend.memory.auto_approval import AUTO_ACTORS, is_auto_approved
from backend.memory.repository import (
    MemoryLifecycleEvent,
    MemoryRepository,
    MemoryRepositoryError,
    MemoryReviewEvent,
    MemoryStatus,
    StoredMemory,
)

_LOG = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

#: The candidate states a person still has to decide on.
CANDIDATE_STATUSES = (MemoryStatus.PENDING, MemoryStatus.CONFLICT)

#: Records that have been approved at some point. Their detail page stays reachable after a note
#: is replaced or retired, so the replacement chain can be followed. Only `approved` reaches
#: the LLM.
NOTE_DETAIL_STATUSES = (MemoryStatus.APPROVED, MemoryStatus.SUPERSEDED, MemoryStatus.RETIRED)

#: Search text limits. The query is a handful of words, never a pattern or an expression.
MAX_QUERY_CHARS = 100
MAX_QUERY_TERMS = 8
#: Unicode categories refused anywhere in the query: control, surrogate, line and paragraph breaks.
_FORBIDDEN_QUERY_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})

_CANONICAL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: The only values an error body may carry in `detail`.
ERROR_INVALID_LIMIT = "invalid_limit"
ERROR_INVALID_STATUS = "invalid_status"
ERROR_INVALID_QUERY = "invalid_query"
ERROR_NOT_FOUND = "not_found"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def memory_dto(stored: StoredMemory) -> dict[str, Any]:
    """The allowlisted public shape of one stored memory (note or candidate)."""
    record = stored.record
    return {
        "id": str(record.id),
        "status": stored.status.value,
        "category": record.category.value,
        "content": record.content,
        "source": record.source,
        "origin": record.origin.value,
        "importance": record.importance,
        "confidence": record.confidence,
        "tags": list(record.tags),
        "project": record.project,
        # The SHA-256 of the vault note at approval; null until a candidate is approved.
        "revision": stored.vault_revision,
        # Set on a correction candidate: the approved note it would replace.
        "supersedes_id": str(stored.supersedes_id) if stored.supersedes_id else None,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def with_auto_flag(dto: dict[str, Any], repository: MemoryRepository, stored: StoredMemory) -> dict:
    """Mark a research or chat note whose approval was automatic (「自動承認(調査/会話)」).

    The key is present, as ``true``, only on such notes, so every other response keeps its shape.
    """
    if (
        stored.status in NOTE_DETAIL_STATUSES
        and stored.record.origin in AUTO_ACTORS
        and is_auto_approved(repository, stored.record.id, stored.record.origin)
    ):
        dto["auto_approved"] = True
    return dto


def _lifecycle_dto(event: MemoryLifecycleEvent) -> dict[str, Any]:
    # `actor` and `reason` are free text a person typed at the review CLI; they are not exposed.
    return {
        "action": event.action,
        "related_id": str(event.related_id) if event.related_id else None,
        "occurred_at": _iso(event.occurred_at),
        "revision": event.vault_revision,
    }


def _review_dto(event: MemoryReviewEvent) -> dict[str, Any]:
    dto: dict[str, Any] = {
        "action": event.action,
        "previous_status": event.previous_status.value,
        "new_status": event.new_status.value,
        "occurred_at": _iso(event.occurred_at),
        "revision": event.vault_revision,
    }
    if event.actor in AUTO_ACTORS.values():
        # The system itself recorded this decision (the actor text is otherwise not exposed).
        dto["automatic"] = True
    return dto


def memory_detail_dto(
    stored: StoredMemory,
    lifecycle: list[MemoryLifecycleEvent],
    reviews: list[MemoryReviewEvent],
) -> dict[str, Any]:
    """The list shape plus the replacement link and the recorded revision history."""
    return {
        **memory_dto(stored),
        # Set on a replaced note: the approved note that took its place.
        "replaced_by_id": str(stored.replaced_by_id) if stored.replaced_by_id else None,
        "lifecycle": [_lifecycle_dto(event) for event in lifecycle],
        "reviews": [_review_dto(event) for event in reviews],
    }


def _parse_query(raw: str | None) -> tuple[str, ...]:
    """Search words. Blank means no search; anything unusual is refused without being echoed."""
    if raw is None:
        return ()
    if len(raw) > MAX_QUERY_CHARS or any(
        unicodedata.category(character) in _FORBIDDEN_QUERY_CATEGORIES for character in raw
    ):
        raise HTTPException(status_code=422, detail=ERROR_INVALID_QUERY)
    terms = tuple(raw.split())
    if len(terms) > MAX_QUERY_TERMS:
        raise HTTPException(status_code=422, detail=ERROR_INVALID_QUERY)
    return terms


def _parse_memory_id(raw: str) -> UUID:
    """Only the canonical lowercase hyphenated form; every other spelling is just not found."""
    if _CANONICAL_ID.fullmatch(raw) is None:
        raise HTTPException(status_code=404, detail=ERROR_NOT_FOUND)
    try:
        memory_id = UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail=ERROR_NOT_FOUND) from None
    if str(memory_id) != raw:
        raise HTTPException(status_code=404, detail=ERROR_NOT_FOUND)
    return memory_id


def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIST_LIMIT
    # isascii() keeps non-ASCII digits (which int() would accept) out of the parser.
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_LIST_LIMIT:
        raise HTTPException(status_code=422, detail=ERROR_INVALID_LIMIT)
    return int(raw)


def _parse_candidate_status(raw: str | None) -> tuple[MemoryStatus, ...]:
    if raw is None:
        return CANDIDATE_STATUSES
    for status in CANDIDATE_STATUSES:
        if raw == status.value:
            return (status,)
    raise HTTPException(status_code=422, detail=ERROR_INVALID_STATUS)


def create_memory_router(repository: MemoryRepository) -> APIRouter:
    """Build the read-only memory router.

    The endpoints are sync functions, so FastAPI runs them in its thread pool and a
    slow disk never blocks the event loop. Each repository call opens and closes
    its own short-lived read-only SQLite connection.
    """
    router = APIRouter()

    def storage_unavailable(exc: Exception) -> HTTPException:
        # Only the exception class is logged: its message could hold stored text.
        _LOG.warning("Memory storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail=ERROR_STORAGE_UNAVAILABLE)

    def newest(
        statuses: tuple[MemoryStatus, ...], limit: int, terms: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        try:
            if terms:
                rows = repository.search_by_status(statuses, terms, limit=limit)
            else:
                rows = [
                    stored
                    for status in statuses
                    for stored in repository.list_by_status(status, limit=limit, newest_first=True)
                ]
                rows.sort(key=lambda s: (s.record.created_at, str(s.record.id)), reverse=True)
            return [
                with_auto_flag(memory_dto(stored), repository, stored) for stored in rows[:limit]
            ]
        except (MemoryRepositoryError, ValueError, KeyError, TypeError) as exc:
            # A row that no longer parses is a storage problem, not the caller's.
            raise storage_unavailable(exc) from exc

    def detail(memory_id: str, statuses: tuple[MemoryStatus, ...]) -> dict[str, Any]:
        parsed = _parse_memory_id(memory_id)
        try:
            stored = repository.get(parsed)
            if stored is None or stored.status not in statuses:
                raise HTTPException(status_code=404, detail=ERROR_NOT_FOUND)
            return with_auto_flag(
                memory_detail_dto(
                    stored, repository.lifecycle_events(parsed), repository.review_events(parsed)
                ),
                repository,
                stored,
            )
        except (MemoryRepositoryError, ValueError, KeyError, TypeError) as exc:
            raise storage_unavailable(exc) from exc

    @router.get("/api/memory/notes")
    def list_notes(limit: str | None = None, q: str | None = None) -> dict[str, Any]:
        terms = _parse_query(q)
        return {"notes": newest((MemoryStatus.APPROVED,), _parse_limit(limit), terms)}

    @router.get("/api/memory/notes/{memory_id}")
    def note_detail(memory_id: str) -> dict[str, Any]:
        return detail(memory_id, NOTE_DETAIL_STATUSES)

    @router.get("/api/memory/candidates")
    def list_candidates(
        status: str | None = None, limit: str | None = None, q: str | None = None
    ) -> dict[str, Any]:
        statuses = _parse_candidate_status(status)
        terms = _parse_query(q)
        return {"candidates": newest(statuses, _parse_limit(limit), terms)}

    @router.get("/api/memory/candidates/{memory_id}")
    def candidate_detail(memory_id: str) -> dict[str, Any]:
        return detail(memory_id, CANDIDATE_STATUSES)

    return router
