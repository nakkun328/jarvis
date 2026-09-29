"""Conservative, key-free staging of conversation and event memory candidates.

The pipeline only proposes memories. It never edits an approved note or treats
unreviewed text as a fact. Topic labels are caller-supplied review keys, not a
claim that two sentences are semantically equivalent.
"""

import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid5

from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.repository import (
    MemoryAlreadyExists,
    MemoryRepository,
    MemoryStateChanged,
    MemoryStatus,
    StoredMemory,
)
from backend.memory.retrieval import MemoryRetriever, RetrievalIssue, RetrievedMemory
from backend.memory.self_memory import SelfMemoryKind
from backend.memory.writer import MemoryWriter

_NAMESPACE = UUID("71ac1066-8039-49db-89d6-91c53c09a89e")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REMEMBER = re.compile(
    r"^\s*remember(?: that)?\s+([a-z0-9][a-z0-9_-]{0,63})\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TOPIC_PREFIX = "consolidation-topic:"


class ConsolidationError(RuntimeError):
    """A source, canonical note, or review operation needs attention."""


class IndexRefreshError(ConsolidationError):
    """Approval succeeded but index refresh failed; retry publication."""


@dataclass(frozen=True)
class ConversationEvidence:
    conversation_id: UUID
    message_id: int
    role: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, UUID):
            raise ValueError("conversation_id must be a UUID")
        if (
            isinstance(self.message_id, bool)
            or not isinstance(self.message_id, int)
            or self.message_id < 1
        ):
            raise ValueError("message_id must be a positive integer")
        if self.role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")

    @property
    def source(self) -> str:
        return f"conversation:{self.conversation_id}:message:{self.message_id}"


@dataclass(frozen=True)
class SelfEvent:
    event_id: str
    kind: SelfMemoryKind
    topic: str
    observation: str
    guidance: str
    source: str
    origin: MemoryOrigin
    importance: float
    confidence: float
    project: str | None = None


@dataclass(frozen=True)
class ExtractedCandidate:
    category: MemoryCategory
    topic: str
    content: str
    source: str
    origin: MemoryOrigin
    importance: float
    confidence: float
    tags: tuple[str, ...] = ()
    project: str | None = None


Evidence = ConversationEvidence | SelfEvent


class CandidateExtractor(Protocol):
    """Exchangeable, offline extractor; extraction does not persist anything."""

    def extract(self, evidence: Evidence) -> Sequence[ExtractedCandidate]: ...


class IndexRefresher(Protocol):
    """Optional adapter that refreshes a reviewed canonical memory by ID.

    The adapter owns embedding generation and its configured vector space.
    """

    def refresh(self, memory: RetrievedMemory) -> None: ...


class ExplicitExtractor:
    """Extract only labelled user statements and typed Self Memory events."""

    def extract(self, evidence: Evidence) -> Sequence[ExtractedCandidate]:
        if isinstance(evidence, ConversationEvidence):
            if evidence.role != "user":
                return ()
            match = _REMEMBER.fullmatch(evidence.text)
            if match is None:
                return ()
            return (
                ExtractedCandidate(
                    category=MemoryCategory.USER,
                    topic=match.group(1).lower(),
                    content=match.group(2).strip(),
                    source=evidence.source,
                    origin=MemoryOrigin.USER_EXPLICIT,
                    importance=0.6,
                    confidence=1.0,
                ),
            )
        if isinstance(evidence, SelfEvent):
            if not isinstance(evidence.kind, SelfMemoryKind):
                raise ValueError("Self event kind must be a SelfMemoryKind")
            if not evidence.event_id.strip() or not evidence.source.strip():
                raise ValueError("Self event ID and source must contain text")
            if not evidence.observation.strip() or not evidence.guidance.strip():
                raise ValueError("Self event observation and guidance must contain text")
            labels = {
                SelfMemoryKind.SUCCESS: ("Succeeded", "Repeat"),
                SelfMemoryKind.FAILURE: ("Failed", "Next time"),
                SelfMemoryKind.CORRECTION: ("Correction", "Use instead"),
            }
            observed, guided = labels[evidence.kind]
            return (
                ExtractedCandidate(
                    category=MemoryCategory.SELF,
                    topic=evidence.topic,
                    content=(
                        f"{observed}: {evidence.observation.strip()}\n"
                        f"{guided}: {evidence.guidance.strip()}"
                    ),
                    source=f"event:{evidence.event_id}:{evidence.source}",
                    origin=evidence.origin,
                    importance=evidence.importance,
                    confidence=evidence.confidence,
                    tags=(f"self-kind:{evidence.kind.value}",),
                    project=evidence.project,
                ),
            )
        raise TypeError("Unsupported consolidation evidence")


@dataclass(frozen=True)
class StageResult:
    pending: tuple[StoredMemory, ...]
    conflicts: tuple[StoredMemory, ...]
    duplicates: tuple[ExtractedCandidate, ...]


class MemoryConsolidator:
    def __init__(
        self,
        database: Database,
        writer: MemoryWriter,
        retriever: MemoryRetriever,
        *,
        extractor: CandidateExtractor | None = None,
        index_refresher: IndexRefresher | None = None,
    ) -> None:
        if (
            writer.repository.database.path != database.path
            or retriever.repository.database.path != database.path
        ):
            raise ValueError("Consolidation components must use the same database")
        self.database = database
        self.writer = writer
        self.retriever = retriever
        self.extractor = extractor or ExplicitExtractor()
        self.index_refresher = index_refresher

    def stage_conversation(self, conversation_id: UUID) -> StageResult:
        """Read successful persisted user turns, then stage explicit candidates."""
        if not isinstance(conversation_id, UUID):
            raise ValueError("conversation_id must be a UUID")
        try:
            with self.database.connect(read_only=True) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                if exists is None:
                    raise ConsolidationError("Conversation does not exist")
                rows = connection.execute(
                    "SELECT id, role, content FROM conversation_messages "
                    "WHERE conversation_id = ? ORDER BY id",
                    (str(conversation_id),),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise ConsolidationError("Conversation storage unavailable") from exc
        evidence = [
            ConversationEvidence(conversation_id, row["id"], row["role"], row["content"])
            for index, row in enumerate(rows[:-1])
            if row["role"] == "user" and rows[index + 1]["role"] == "assistant"
        ]
        return self.stage(evidence)

    def stage(self, evidence: Iterable[Evidence]) -> StageResult:
        """Preflight canonical vault notes; stage only new, reviewable candidates."""
        extracted = [candidate for item in evidence for candidate in self.extractor.extract(item)]
        prepared = [_prepare(candidate) for candidate in extracted]
        prepared.sort(key=lambda item: (item[0].source, item[0].category.value, item[1]))
        known = self._snapshot()  # Fail closed before writes if any approved note is invalid.
        staged_ids: list[UUID] = []
        duplicates: list[ExtractedCandidate] = []
        for candidate, topic, record in prepared:
            peers = [
                item
                for item in known
                if item.record.category is record.category
                and item.record.project == record.project
            ]
            if any(
                _normalize(item.record.content) == _normalize(record.content)
                and item.record.origin is record.origin
                for item in peers
            ):
                duplicates.append(candidate)
                continue
            conflicting = any(
                _topic_of(item.record) in (None, topic)
                and _normalize(item.record.content) != _normalize(record.content)
                for item in peers
                if item.status is not MemoryStatus.REJECTED
            )
            if conflicting:
                for item in peers:
                    if (
                        item.status is MemoryStatus.PENDING
                        and _topic_of(item.record) == topic
                        and _normalize(item.record.content) != _normalize(record.content)
                    ):
                        try:
                            self.writer.repository.transition(
                                item.record.id,
                                expected=MemoryStatus.PENDING,
                                new=MemoryStatus.CONFLICT,
                            )
                        except MemoryStateChanged:
                            pass  # The next snapshot observes the concurrent review state.
            try:
                stored = self.writer.submit(record)
            except MemoryAlreadyExists:
                stored = self.writer.repository.get(record.id)
                if (
                    stored is None
                    or _normalize(stored.record.content) != _normalize(record.content)
                    or stored.record.origin is not record.origin
                ):
                    raise ConsolidationError("Deterministic candidate ID collision") from None
            if conflicting and stored.status is MemoryStatus.PENDING:
                try:
                    stored = self.writer.repository.transition(
                        record.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
                    )
                except MemoryStateChanged:
                    stored = self.writer.repository.get(record.id)
                    if stored is None:
                        raise ConsolidationError("Candidate disappeared during review") from None
            staged_ids.append(stored.record.id)
            known.append(stored)
        final = [self.writer.repository.get(memory_id) for memory_id in staged_ids]
        return StageResult(
            tuple(item for item in final if item and item.status is MemoryStatus.PENDING),
            tuple(item for item in final if item and item.status is MemoryStatus.CONFLICT),
            tuple(duplicates),
        )

    def publish_reviewed(self, memory_id: UUID) -> StoredMemory:
        """Explicit reviewer action; retry also retries a failed index refresh."""
        stored = self.writer.approve(memory_id)
        canonical = self.retriever.get_approved(memory_id)
        if not isinstance(canonical, RetrievedMemory):
            raise ConsolidationError("Approved note cannot be resolved for index refresh")
        if self.index_refresher is not None:
            try:
                self.index_refresher.refresh(canonical)
            except Exception as exc:
                raise IndexRefreshError(
                    "Memory was approved but index refresh failed; retry publish_reviewed"
                ) from exc
        return stored

    def _snapshot(self) -> list[StoredMemory]:
        known: list[StoredMemory] = []
        repository: MemoryRepository = self.writer.repository
        for status in MemoryStatus:
            after_id: UUID | None = None
            while True:
                page = repository.page_by_status(status, after_id=after_id)
                if not page:
                    break
                for stored in page:
                    if status is MemoryStatus.APPROVED:
                        canonical = self.retriever.get_approved(stored.record.id)
                        if isinstance(canonical, RetrievalIssue) or canonical is None:
                            raise ConsolidationError(
                                f"Approved note {stored.record.id} needs repair "
                                "before consolidation"
                            )
                        known.append(
                            StoredMemory(canonical.record, status, canonical.note_revision)
                        )
                    else:
                        known.append(stored)
                after_id = page[-1].record.id
        return known


def _prepare(candidate: ExtractedCandidate) -> tuple[ExtractedCandidate, str, MemoryRecord]:
    if not isinstance(candidate, ExtractedCandidate):
        raise ValueError("Extractor must return ExtractedCandidate values")
    topic = candidate.topic.strip().lower()
    if not _TOPIC.fullmatch(topic):
        raise ValueError("Candidate topic must be an ASCII label of at most 64 characters")
    if any(tag.startswith(_TOPIC_PREFIX) for tag in candidate.tags):
        raise ValueError("Consolidation topic tags are reserved")
    normalized = _normalize(candidate.content)
    identity = "\x1f".join(
        (
            candidate.category.value,
            candidate.project or "",
            topic,
            normalized,
            candidate.origin.value,
        )
    )
    record = MemoryRecord(
        id=uuid5(_NAMESPACE, identity),
        category=candidate.category,
        content=candidate.content.strip(),
        source=candidate.source,
        origin=candidate.origin,
        importance=candidate.importance,
        confidence=candidate.confidence,
        tags=(_TOPIC_PREFIX + topic, *candidate.tags),
        project=candidate.project,
    )
    return candidate, topic, record


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _topic_of(record: MemoryRecord) -> str | None:
    topics = [tag[len(_TOPIC_PREFIX):] for tag in record.tags if tag.startswith(_TOPIC_PREFIX)]
    return topics[0] if len(topics) == 1 else None
