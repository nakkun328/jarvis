"""Resolve searchable memory IDs through SQLite and the editable vault."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from backend.memory.model import MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault, VaultDocument, VaultError, VaultFormatError
from backend.memory.repository import MemoryRepository, MemoryStatus, StoredMemory
from backend.memory.vector import VectorIndex, VectorQuery

_PAGE_SIZE = 100
_TOKENS = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class RetrievedMemory:
    record: MemoryRecord
    note_revision: str
    edited_since_approval: bool
    stale: bool
    match_score: float
    match_kind: Literal["text", "vector"]


@dataclass(frozen=True)
class RetrievalIssue:
    memory_id: UUID
    reason: Literal["missing_note", "invalid_note", "invalid_metadata", "vault_unavailable"]


@dataclass(frozen=True)
class RetrievalResult:
    matches: tuple[RetrievedMemory, ...]
    conflicts: tuple[StoredMemory, ...]
    issues: tuple[RetrievalIssue, ...]


class MemoryRetriever:
    """Search approved memories while keeping conflicts and failures separate.

    Text search scans current vault notes for a small personal corpus. Vector
    search uses a supplied index only for candidate IDs; neither index nor the
    SQLite candidate snapshot is the source of approved memory content.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        vault: ObsidianVault,
        *,
        vector_index: VectorIndex | None = None,
        freshness_window: timedelta = timedelta(days=180),
    ) -> None:
        if freshness_window <= timedelta(0):
            raise ValueError("freshness_window must be positive")
        self.repository = repository
        self.vault = vault
        self.vector_index = vector_index
        self.freshness_window = freshness_window

    def search_text(
        self, query: str, *, limit: int = 10, as_of: datetime | None = None
    ) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must contain text")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        now = _as_of(as_of)
        matches: list[RetrievedMemory] = []
        issues: list[RetrievalIssue] = []
        for stored in self._iter_status(MemoryStatus.APPROVED):
            resolved = self._resolve(stored, now)
            if isinstance(resolved, RetrievalIssue):
                issues.append(resolved)
                continue
            score = _text_score(query, resolved.record)
            if score > 0:
                matches.append(replace(resolved, match_score=score, match_kind="text"))
        matches.sort(
            key=lambda item: (
                item.match_score,
                item.record.origin is MemoryOrigin.USER_EXPLICIT,
                not item.stale,
                item.record.importance,
                item.record.confidence,
                item.record.updated_at,
            ),
            reverse=True,
        )
        conflicts = tuple(
            stored
            for stored in self._iter_status(MemoryStatus.CONFLICT)
            if _text_score(query, stored.record) > 0
        )
        return RetrievalResult(tuple(matches[:limit]), conflicts, tuple(issues))

    async def search_vector(
        self, query: VectorQuery, *, as_of: datetime | None = None
    ) -> RetrievalResult:
        if self.vector_index is None:
            raise ValueError("vector_index is required for vector search")
        now = _as_of(as_of)
        matches: list[RetrievedMemory] = []
        conflicts: list[StoredMemory] = []
        issues: list[RetrievalIssue] = []
        seen: set[UUID] = set()
        candidate_limit = query.limit
        while True:
            # The index cannot filter on canonical review status. Expand the
            # candidate window when pending, missing or invalid rows occupy it.
            candidates = await self.vector_index.search(replace(query, limit=candidate_limit))
            for match in candidates:
                try:
                    memory_id = UUID(match.memory_id)
                except ValueError:
                    continue
                if memory_id in seen:
                    continue
                seen.add(memory_id)
                stored = self.repository.get(memory_id)
                if stored is None:
                    continue
                if stored.status is MemoryStatus.CONFLICT:
                    conflicts.append(stored)
                elif stored.status is MemoryStatus.APPROVED:
                    resolved = self._resolve(stored, now)
                    if isinstance(resolved, RetrievalIssue):
                        issues.append(resolved)
                    else:
                        matches.append(
                            replace(resolved, match_score=match.score, match_kind="vector")
                        )
                if len(matches) >= query.limit:
                    break
            if len(matches) >= query.limit or len(candidates) < candidate_limit:
                break
            candidate_limit *= 2
        return RetrievalResult(tuple(matches), tuple(conflicts), tuple(issues))

    def _iter_status(self, status: MemoryStatus) -> Iterator[StoredMemory]:
        after_id: UUID | None = None
        while True:
            page = self.repository.page_by_status(status, after_id=after_id, limit=_PAGE_SIZE)
            if not page:
                return
            yield from page
            after_id = page[-1].record.id

    def _resolve(self, stored: StoredMemory, now: datetime) -> RetrievedMemory | RetrievalIssue:
        try:
            note = self.vault.read(stored.record.id)
        except VaultFormatError:
            return RetrievalIssue(stored.record.id, "invalid_note")
        except (OSError, VaultError):
            return RetrievalIssue(stored.record.id, "vault_unavailable")
        if note is None:
            return RetrievalIssue(stored.record.id, "missing_note")
        try:
            record = _effective_record(stored.record, note)
        except (AttributeError, KeyError, TypeError, ValueError):
            return RetrievalIssue(stored.record.id, "invalid_metadata")
        return RetrievedMemory(
            record=record,
            note_revision=note.revision,
            edited_since_approval=note.revision != stored.vault_revision,
            stale=now - record.updated_at > self.freshness_window,
            match_score=0.0,
            match_kind="text",
        )


def _effective_record(stored: MemoryRecord, note: VaultDocument) -> MemoryRecord:
    metadata = note.metadata
    created_at = metadata["created_at"]
    if not isinstance(created_at, str) or datetime.fromisoformat(created_at) != stored.created_at:
        raise ValueError("Vault creation time differs from SQLite")
    if (
        metadata["category"] != stored.category.value
        or metadata["source"] != stored.source
        or metadata["origin"] != stored.origin.value
    ):
        raise ValueError("Vault provenance differs from SQLite")
    tags = metadata["tags"]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Invalid vault tags")
    project = metadata["project"]
    if project is not None and not isinstance(project, str):
        raise ValueError("Invalid vault project")
    return replace(
        stored,
        content=note.body,
        importance=metadata["importance"],
        confidence=metadata["confidence"],
        tags=tuple(tags),
        project=project,
    )


def _text_score(query: str, record: MemoryRecord) -> float:
    needle = query.strip().casefold()
    haystack = " ".join((record.content, *record.tags, record.project or "")).casefold()
    if needle in haystack:
        return 1.0
    terms = set(_TOKENS.findall(needle))
    if not terms:
        return 0.0
    return len(terms & set(_TOKENS.findall(haystack))) / len(terms)


def _as_of(value: datetime | None) -> datetime:
    now = value or datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("as_of must have a timezone")
    return now
