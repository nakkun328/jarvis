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


class MemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, record: MemoryRecord) -> StoredMemory:
        """Save an unreviewed candidate without replacing an existing memory."""
        try:
            with self.database.connect() as connection, connection:
                connection.execute(
                    "INSERT INTO memory_records ("
                    "id, category, content, source, origin, importance, confidence, "
                    "created_at, updated_at, last_accessed, tags, project, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise MemoryAlreadyExists("Memory candidate ID already exists") from exc
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return StoredMemory(record, MemoryStatus.PENDING, None)

    def get(self, memory_id: UUID) -> StoredMemory | None:
        try:
            with self.database.connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT * FROM memory_records WHERE id = ?", (str(memory_id),)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return _stored(row) if row is not None else None

    def list_by_status(self, status: MemoryStatus, *, limit: int = 100) -> list[StoredMemory]:
        if not isinstance(status, MemoryStatus):
            raise ValueError("status must be a MemoryStatus")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        try:
            with self.database.connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT * FROM memory_records WHERE status = ? "
                    "ORDER BY created_at, id LIMIT ?",
                    (status.value, limit),
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
    ) -> StoredMemory:
        """Compare and swap a review state; approval requires a persisted vault note."""
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
        try:
            with self.database.connect() as connection, connection:
                updated = connection.execute(
                    "UPDATE memory_records SET status = ?, vault_revision = ?, "
                    "updated_at = MAX(updated_at, ?) "
                    "WHERE id = ? AND status = ?",
                    (
                        new.value,
                        vault_revision,
                        datetime.now(UTC).isoformat(),
                        str(memory_id),
                        expected.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise MemoryStateChanged("Memory candidate missing or state changed")
                row = connection.execute(
                    "SELECT * FROM memory_records WHERE id = ?", (str(memory_id),)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise MemoryRepositoryError("Memory storage unavailable") from exc
        return _stored(row)


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
    return StoredMemory(record, MemoryStatus(row["status"]), row["vault_revision"])
