"""Durable memory candidates and explicit review state.

This repository does not infer approval or write vault notes. The later writer
coordinates vault creation and then records its revision here.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord


class MemoryStatus(StrEnum):
    PENDING = "pending"
    CONFLICT = "conflict"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class MemoryRepositoryError(RuntimeError):
    """A candidate could not be stored or read."""


class MemoryAlreadyExists(MemoryRepositoryError):
    """A candidate with the same ID already exists."""


class MemoryStateChanged(MemoryRepositoryError):
    """The candidate is missing or its review state changed."""


@dataclass(frozen=True)
class StoredMemory:
    record: MemoryRecord
    status: MemoryStatus
    vault_revision: str | None
    supersedes_id: UUID | None = None
    supersedes_revision: str | None = None
    replaced_by_id: UUID | None = None


@dataclass(frozen=True)
class MemoryLifecycleEvent:
    id: int
    memory_id: UUID
    related_id: UUID | None
    action: str
    actor: str
    reason: str
    occurred_at: datetime
    vault_revision: str


@dataclass(frozen=True)
class MemoryReviewEvent:
    id: int
    memory_id: UUID
    previous_status: MemoryStatus
    new_status: MemoryStatus
    action: str
    actor: str
    occurred_at: datetime
    vault_revision: str | None


def validate_review_actor(actor: str | None) -> str:
    """Normalize caller attribution; missing identity remains explicitly unknown."""
    if actor is None:
        return "unknown"
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or len(actor) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in actor)
    ):
        raise ValueError("actor must be nonempty printable text of at most 256 characters")
    return actor.strip()


class MemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        record: MemoryRecord,
        *,
        supersedes_id: UUID | None = None,
        supersedes_revision: str | None = None,
    ) -> StoredMemory:
        """Save an unreviewed candidate without replacing an existing memory."""
        if (supersedes_id is None) != (supersedes_revision is None):
            raise ValueError("Correction requires an old ID and observed note revision")
        if supersedes_id is not None and not isinstance(supersedes_id, UUID):
            raise ValueError("Original memory ID must be a UUID")
        if supersedes_id == record.id:
            raise ValueError("Memory cannot supersede itself")
        if supersedes_revision is not None and (
            not isinstance(supersedes_revision, str) or not supersedes_revision.strip()
        ):
            raise ValueError("Correction requires a nonblank note revision")
        try:
            with self.database.connect() as connection, connection:
                if supersedes_id is not None:
                    old = connection.execute(
                        "SELECT status, category FROM memory_records WHERE id = ?",
                        (str(supersedes_id),),
                    ).fetchone()
                    if old is None or old["status"] != MemoryStatus.APPROVED.value:
                        raise MemoryStateChanged("Original memory is no longer approved")
                    if old["category"] != record.category.value:
                        raise ValueError("Correction must keep the original memory category")
                connection.execute(
                    "INSERT INTO memory_records ("
                    "id, category, content, source, origin, importance, confidence, "
                    "created_at, updated_at, last_accessed, tags, project, status, "
                    "supersedes_id, supersedes_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(record.id),
                        record.category.value,
                        record.content,
                        record.source,
                        record.origin.value,
                        record.importance,
                        record.confidence,
                        record.created_at.astimezone(UTC).isoformat(),
                        record.updated_at.astimezone(UTC).isoformat(),
                        record.last_accessed.astimezone(UTC).isoformat()
                        if record.last_accessed
                        else None,
                        json.dumps(record.tags, ensure_ascii=False),
                        record.project,
                        MemoryStatus.PENDING.value,
                        str(supersedes_id) if supersedes_id else None,
                        supersedes_revision,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MemoryAlreadyExists("Memory candidate ID already exists") from exc
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return StoredMemory(record, MemoryStatus.PENDING, None, supersedes_id, supersedes_revision)

    def get(self, memory_id: UUID) -> StoredMemory | None:
        try:
            with self.database.connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT * FROM memory_records WHERE id = ?", (str(memory_id),)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return _stored(row) if row is not None else None

    def list_by_status(
        self, status: MemoryStatus, *, limit: int = 100, newest_first: bool = False
    ) -> list[StoredMemory]:
        if not isinstance(status, MemoryStatus):
            raise ValueError("status must be a MemoryStatus")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        order = "created_at DESC, id DESC" if newest_first else "created_at, id"
        try:
            with self.database.connect(read_only=True) as connection:
                rows = connection.execute(
                    f"SELECT * FROM memory_records WHERE status = ? ORDER BY {order} LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return [_stored(row) for row in rows]

    def page_by_status(
        self, status: MemoryStatus, *, after_id: UUID | None = None, limit: int = 100
    ) -> list[StoredMemory]:
        """Page by stable ID order so retrieval can inspect the full corpus."""
        if not isinstance(status, MemoryStatus):
            raise ValueError("status must be a MemoryStatus")
        if after_id is not None and not isinstance(after_id, UUID):
            raise ValueError("after_id must be a UUID")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        try:
            with self.database.connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT * FROM memory_records WHERE status = ? AND id > ? ORDER BY id LIMIT ?",
                    (status.value, str(after_id) if after_id else "", limit),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return [_stored(row) for row in rows]

    def transition(
        self,
        memory_id: UUID,
        *,
        expected: MemoryStatus,
        new: MemoryStatus,
        vault_revision: str | None = None,
        actor: str | None = None,
    ) -> StoredMemory:
        """Atomically record a compare-and-swap transition and its audit event."""
        if not isinstance(expected, MemoryStatus) or not isinstance(new, MemoryStatus):
            raise ValueError("expected and new must be MemoryStatus values")
        allowed = {
            MemoryStatus.PENDING: {
                MemoryStatus.CONFLICT,
                MemoryStatus.REJECTED,
                MemoryStatus.APPROVED,
            },
            MemoryStatus.CONFLICT: {MemoryStatus.REJECTED, MemoryStatus.APPROVED},
        }
        if new not in allowed.get(expected, set()):
            raise ValueError("Invalid memory state transition")
        if new is MemoryStatus.APPROVED:
            if not vault_revision or not vault_revision.strip():
                raise ValueError("Approval requires a vault revision")
        elif vault_revision is not None:
            raise ValueError("Only approval accepts a vault revision")
        actor_name = validate_review_actor(actor)
        occurred_at = datetime.now(UTC).isoformat()
        action = {
            MemoryStatus.CONFLICT: "flag_conflict",
            MemoryStatus.APPROVED: "approve",
            MemoryStatus.REJECTED: "reject",
        }[new]
        try:
            with self.database.connect() as connection, connection:
                candidate = connection.execute(
                    "SELECT supersedes_id, supersedes_revision FROM memory_records "
                    "WHERE id = ? AND status = ?",
                    (str(memory_id), expected.value),
                ).fetchone()
                if candidate is None:
                    raise MemoryStateChanged("Memory candidate missing or state changed")
                if new is MemoryStatus.APPROVED and candidate["supersedes_id"] is not None:
                    old = connection.execute(
                        "UPDATE memory_records SET status = ?, replaced_by_id = ?, "
                        "updated_at = MAX(updated_at, ?) "
                        "WHERE id = ? AND status = ?",
                        (
                            MemoryStatus.SUPERSEDED.value,
                            str(memory_id),
                            occurred_at,
                            candidate["supersedes_id"],
                            MemoryStatus.APPROVED.value,
                        ),
                    )
                    if old.rowcount != 1:
                        raise MemoryStateChanged("Original memory changed before approval")
                    connection.execute(
                        "INSERT INTO memory_lifecycle_events "
                        "(memory_id, related_id, action, actor, reason, occurred_at, "
                        "vault_revision) VALUES (?, ?, 'supersede', ?, ?, ?, ?)",
                        (
                            candidate["supersedes_id"],
                            str(memory_id),
                            actor_name,
                            "reviewed correction",
                            occurred_at,
                            candidate["supersedes_revision"],
                        ),
                    )
                updated = connection.execute(
                    "UPDATE memory_records SET status = ?, vault_revision = ?, "
                    "updated_at = MAX(updated_at, ?) "
                    "WHERE id = ? AND status = ?",
                    (
                        new.value,
                        vault_revision,
                        occurred_at,
                        str(memory_id),
                        expected.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise MemoryStateChanged("Memory candidate missing or state changed")
                connection.execute(
                    "INSERT INTO memory_review_events ("
                    "memory_id, previous_status, new_status, action, actor, "
                    "occurred_at, vault_revision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(memory_id),
                        expected.value,
                        new.value,
                        action,
                        actor_name,
                        occurred_at,
                        vault_revision,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM memory_records WHERE id = ?", (str(memory_id),)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return _stored(row)

    def retire(
        self, memory_id: UUID, *, vault_revision: str, actor: str, reason: str
    ) -> StoredMemory:
        """Explicitly retire an approved memory while preserving its note and history."""
        actor_name = validate_review_actor(actor)
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1000
            or any(ord(character) < 32 or ord(character) == 127 for character in reason)
        ):
            raise ValueError("Retirement reason must be printable text of 1 to 1000 characters")
        if not isinstance(vault_revision, str) or not vault_revision.strip():
            raise ValueError("Retirement requires observed note revision")
        occurred_at = datetime.now(UTC).isoformat()
        try:
            with self.database.connect() as connection, connection:
                updated = connection.execute(
                    "UPDATE memory_records SET status = ?, updated_at = MAX(updated_at, ?) "
                    "WHERE id = ? AND status = ?",
                    (
                        MemoryStatus.RETIRED.value,
                        occurred_at,
                        str(memory_id),
                        MemoryStatus.APPROVED.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise MemoryStateChanged("Memory is no longer approved")
                connection.execute(
                    "INSERT INTO memory_lifecycle_events "
                    "(memory_id, related_id, action, actor, reason, occurred_at, "
                    "vault_revision) VALUES (?, NULL, 'retire', ?, ?, ?, ?)",
                    (str(memory_id), actor_name, reason.strip(), occurred_at, vault_revision),
                )
                row = connection.execute(
                    "SELECT * FROM memory_records WHERE id = ?", (str(memory_id),)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return _stored(row)

    def lifecycle_events(self, memory_id: UUID) -> list[MemoryLifecycleEvent]:
        if not isinstance(memory_id, UUID):
            raise ValueError("memory_id must be a UUID")
        try:
            with self.database.connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT * FROM memory_lifecycle_events WHERE memory_id = ? ORDER BY id",
                    (str(memory_id),),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return [
            MemoryLifecycleEvent(
                row["id"],
                UUID(row["memory_id"]),
                UUID(row["related_id"]) if row["related_id"] else None,
                row["action"],
                row["actor"],
                row["reason"],
                datetime.fromisoformat(row["occurred_at"]),
                row["vault_revision"],
            )
            for row in rows
        ]

    def review_events(self, memory_id: UUID) -> list[MemoryReviewEvent]:
        """Return committed candidate review transitions in insertion order."""
        if not isinstance(memory_id, UUID):
            raise ValueError("memory_id must be a UUID")
        try:
            with self.database.connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT * FROM memory_review_events WHERE memory_id = ? ORDER BY id",
                    (str(memory_id),),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return [
            MemoryReviewEvent(
                id=row["id"],
                memory_id=UUID(row["memory_id"]),
                previous_status=MemoryStatus(row["previous_status"]),
                new_status=MemoryStatus(row["new_status"]),
                action=row["action"],
                actor=row["actor"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                vault_revision=row["vault_revision"],
            )
            for row in rows
        ]


def _stored(row: sqlite3.Row) -> StoredMemory:
    record = MemoryRecord(
        id=UUID(row["id"]),
        category=MemoryCategory(row["category"]),
        content=row["content"],
        source=row["source"],
        origin=MemoryOrigin(row["origin"]),
        importance=row["importance"],
        confidence=row["confidence"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_accessed=(
            datetime.fromisoformat(row["last_accessed"]) if row["last_accessed"] else None
        ),
        tags=tuple(json.loads(row["tags"])),
        project=row["project"],
    )
    return StoredMemory(
        record,
        MemoryStatus(row["status"]),
        row["vault_revision"],
        UUID(row["supersedes_id"]) if row["supersedes_id"] else None,
        row["supersedes_revision"],
        UUID(row["replaced_by_id"]) if row["replaced_by_id"] else None,
    )
