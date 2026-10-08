"""Read-only memory endpoints: reviewed notes and pending candidates.

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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from backend.memory.repository import (
    MemoryRepository,
    MemoryRepositoryError,
    MemoryStatus,
    StoredMemory,
)

_LOG = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

#: The candidate states a person still has to decide on.
CANDIDATE_STATUSES = (MemoryStatus.PENDING, MemoryStatus.CONFLICT)

#: The only values an error body may carry in `detail`.
ERROR_INVALID_LIMIT = "invalid_limit"
ERROR_INVALID_STATUS = "invalid_status"
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

    def newest(statuses: tuple[MemoryStatus, ...], limit: int) -> list[dict[str, Any]]:
        try:
            rows = [
                stored
                for status in statuses
                for stored in repository.list_by_status(status, limit=limit, newest_first=True)
            ]
            rows.sort(key=lambda s: (s.record.created_at, str(s.record.id)), reverse=True)
            return [memory_dto(stored) for stored in rows[:limit]]
        except (MemoryRepositoryError, ValueError, KeyError, TypeError) as exc:
            # A row that no longer parses is a storage problem, not the caller's.
            raise storage_unavailable(exc) from exc

    @router.get("/api/memory/notes")
    def list_notes(limit: str | None = None) -> dict[str, Any]:
        return {"notes": newest((MemoryStatus.APPROVED,), _parse_limit(limit))}

    @router.get("/api/memory/candidates")
    def list_candidates(status: str | None = None, limit: str | None = None) -> dict[str, Any]:
        statuses = _parse_candidate_status(status)
        return {"candidates": newest(statuses, _parse_limit(limit))}

    return router
