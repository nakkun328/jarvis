"""SQLite candidate lifecycle and additive schema migration."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from backend.core.database import SCHEMA_VERSION, Database, DatabaseError
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.repository import (
    MemoryAlreadyExists,
    MemoryRepository,
    MemoryRepositoryError,
    MemoryStateChanged,
    MemoryStatus,
)


def _candidate() -> MemoryRecord:
    now = datetime.now(UTC)
    return MemoryRecord(
        category=MemoryCategory.USER,
        content="Prefers concise replies",
        source="conversation:123",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
        created_at=now,
        updated_at=now,
        tags=("communication",),
    )


def _restore_v4_layout(database: Database) -> None:
    """Build a real v4 parent table for upgrade tests from a fresh fixture."""
    with database.connect() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with connection:
            for table in (
                "task_steps",
                "tasks",
                "research_conflicts",
                "research_source_reasons",
                "research_claims",
                "research_sources",
                "research_queries",
                "research_sessions",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DROP TABLE memory_lifecycle_events")
            connection.execute("DROP INDEX memory_records_by_status")
            connection.execute("DROP INDEX memory_records_by_supersedes")
            connection.execute(
                "CREATE TABLE memory_records_v4 ("
                "id TEXT PRIMARY KEY, category TEXT NOT NULL, content TEXT NOT NULL, "
                "source TEXT NOT NULL, origin TEXT NOT NULL, importance REAL NOT NULL, "
                "confidence REAL NOT NULL, created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, last_accessed TEXT, tags TEXT NOT NULL, "
                "project TEXT, status TEXT NOT NULL CHECK(status IN "
                "('pending', 'conflict', 'approved', 'rejected')), vault_revision TEXT)"
            )
            connection.execute(
                "INSERT INTO memory_records_v4 SELECT id, category, content, source, "
                "origin, importance, confidence, created_at, updated_at, "
                "last_accessed, tags, project, status, vault_revision FROM memory_records"
            )
            connection.execute("DROP TABLE memory_records")
            connection.execute("ALTER TABLE memory_records_v4 RENAME TO memory_records")
            connection.execute(
                "CREATE INDEX memory_records_by_status ON memory_records(status, created_at)"
            )
            connection.execute("DELETE FROM schema_migrations WHERE version IN (5, 6, 7, 8)")
            connection.execute("PRAGMA user_version = 4")
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_candidate_persists_across_repository_instances_and_requires_review(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    candidate = _candidate()
    repository = MemoryRepository(database)

    saved = repository.add(candidate)
    assert saved.status is MemoryStatus.PENDING
    assert saved.vault_revision is None
    assert MemoryRepository(database).get(candidate.id) == saved
    with pytest.raises(MemoryAlreadyExists):
        repository.add(replace(candidate, content="Conflicting replacement"))
    assert repository.get(candidate.id) == saved

    flagged = repository.transition(
        candidate.id,
        expected=MemoryStatus.PENDING,
        new=MemoryStatus.CONFLICT,
        actor="reviewer:alice",
    )
    assert flagged.status is MemoryStatus.CONFLICT
    assert repository.list_by_status(MemoryStatus.PENDING) == []
    assert repository.list_by_status(MemoryStatus.CONFLICT) == [flagged]
    with pytest.raises(MemoryStateChanged):
        repository.transition(
            candidate.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED
        )
    with pytest.raises(ValueError, match="vault revision"):
        repository.transition(
            candidate.id, expected=MemoryStatus.CONFLICT, new=MemoryStatus.APPROVED
        )
    approved = repository.transition(
        candidate.id,
        expected=MemoryStatus.CONFLICT,
        new=MemoryStatus.APPROVED,
        vault_revision="revision-1",
    )
    assert approved.record.content == candidate.content
    assert approved.record.updated_at >= candidate.updated_at
    assert approved.vault_revision == "revision-1"
    assert MemoryRepository(database).list_by_status(MemoryStatus.APPROVED) == [approved]
    events = MemoryRepository(database).review_events(candidate.id)
    assert [
        (event.previous_status, event.new_status, event.action, event.actor) for event in events
    ] == [
        (MemoryStatus.PENDING, MemoryStatus.CONFLICT, "flag_conflict", "reviewer:alice"),
        (MemoryStatus.CONFLICT, MemoryStatus.APPROVED, "approve", "unknown"),
    ]
    assert events[0].occurred_at.tzinfo == UTC
    assert events[0].vault_revision is None
    assert events[1].vault_revision == "revision-1"
    with pytest.raises(ValueError, match="Invalid memory state transition"):
        repository.transition(
            candidate.id, expected=MemoryStatus.APPROVED, new=MemoryStatus.REJECTED
        )


def test_v2_migration_preserves_conversation_and_records_v3(tmp_path: Path) -> None:
    database = Database(tmp_path / "upgrade.sqlite3")
    database.initialize()
    _restore_v4_layout(database)
    conversation_id = str(uuid4())
    with database.connect() as connection, connection:
        connection.execute(
            "INSERT INTO conversations (id, created_at, updated_at) VALUES (?, 'now', 'now')",
            (conversation_id,),
        )
        connection.execute("DROP INDEX memory_review_events_by_memory")
        connection.execute("DROP TABLE memory_review_events")
        connection.execute("DROP INDEX memory_records_by_status")
        connection.execute("DROP TABLE memory_records")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (3, 4)")
        connection.execute("PRAGMA user_version = 2")

    database.initialize()
    with database.connect(read_only=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT id FROM conversations").fetchone()[0] == conversation_id
        assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations")] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
        ]
    assert MemoryRepository(database).add(_candidate()).status is MemoryStatus.PENDING


def test_v3_migration_preserves_approved_memory_and_does_not_invent_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "upgrade-v3.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    candidate = _candidate()
    repository.add(candidate)
    repository.transition(
        candidate.id,
        expected=MemoryStatus.PENDING,
        new=MemoryStatus.APPROVED,
        vault_revision="existing-revision",
    )
    _restore_v4_layout(database)
    with database.connect() as connection, connection:
        connection.execute("DROP INDEX memory_review_events_by_memory")
        connection.execute("DROP TABLE memory_review_events")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute("PRAGMA user_version = 3")

    database.initialize()
    assert repository.get(candidate.id).status is MemoryStatus.APPROVED
    assert repository.get(candidate.id).vault_revision == "existing-revision"
    assert repository.review_events(candidate.id) == []


def test_v4_to_v5_migration_preserves_review_audit_and_foreign_keys(tmp_path: Path) -> None:
    database = Database(tmp_path / "upgrade-v4.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    candidate = _candidate()
    repository.add(candidate)
    repository.transition(
        candidate.id,
        expected=MemoryStatus.PENDING,
        new=MemoryStatus.APPROVED,
        vault_revision="existing-revision",
        actor="reviewer:migration",
    )
    _restore_v4_layout(database)
    database.initialize()
    assert repository.get(candidate.id).status is MemoryStatus.APPROVED
    assert repository.get(candidate.id).vault_revision == "existing-revision"
    assert [(event.action, event.actor) for event in repository.review_events(candidate.id)] == [
        ("approve", "reviewer:migration")
    ]
    with database.connect(read_only=True) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v5_migration_failure_rolls_back_parent_table_and_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "failed-v5.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    candidate = _candidate()
    repository.add(candidate)
    _restore_v4_layout(database)
    with database.connect() as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_v5 BEFORE INSERT ON schema_migrations "
            "WHEN NEW.version = 5 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
    with pytest.raises(DatabaseError, match="Could not initialize"):
        database.initialize()
    with database.connect(read_only=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations")] == [
            1, 2, 3, 4
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT id FROM memory_records").fetchone()[0] == str(
            candidate.id
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memory_lifecycle_events'"
            ).fetchone()
            is None
        )


def test_v4_migration_failure_rolls_back_schema_and_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "failed-v4.sqlite3")
    database.initialize()
    _restore_v4_layout(database)
    with database.connect() as connection, connection:
        connection.execute("DROP INDEX memory_review_events_by_memory")
        connection.execute("DROP TABLE memory_review_events")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute("PRAGMA user_version = 3")
        connection.execute(
            "CREATE TRIGGER fail_v4 BEFORE INSERT ON schema_migrations "
            "WHEN NEW.version = 4 BEGIN SELECT RAISE(ABORT, 'simulated migration failure'); END"
        )

    with pytest.raises(DatabaseError, match="Could not initialize"):
        database.initialize()
    with database.connect(read_only=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations")] == [
            1,
            2,
            3,
        ]
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memory_review_events'"
            ).fetchone()
            is None
        )


def test_failed_audit_insert_rolls_back_state_and_cas_records_no_event(tmp_path: Path) -> None:
    database = Database(tmp_path / "failed-event.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    candidate = _candidate()
    repository.add(candidate)
    with database.connect() as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_event BEFORE INSERT ON memory_review_events "
            "BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END"
        )

    with pytest.raises(MemoryRepositoryError, match="unavailable"):
        repository.transition(
            candidate.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED
        )
    assert repository.get(candidate.id).status is MemoryStatus.PENDING
    assert repository.review_events(candidate.id) == []
    with database.connect() as connection, connection:
        connection.execute("DROP TRIGGER fail_event")
    repository.transition(candidate.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED)
    with pytest.raises(MemoryStateChanged):
        repository.transition(
            candidate.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
        )
    assert [(event.action, event.actor) for event in repository.review_events(candidate.id)] == [
        ("reject", "unknown")
    ]


def test_actor_validation_does_not_change_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-actor.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    candidate = _candidate()
    repository.add(candidate)
    for actor in (" ", "alice\nadmin", "a" * 257, 42):
        with pytest.raises(ValueError, match="actor"):
            repository.transition(
                candidate.id,
                expected=MemoryStatus.PENDING,
                new=MemoryStatus.REJECTED,
                actor=actor,
            )
    assert repository.get(candidate.id).status is MemoryStatus.PENDING
    assert repository.review_events(candidate.id) == []


def test_missing_database_is_not_recreated_by_memory_repository(tmp_path: Path) -> None:
    path = tmp_path / "removed.sqlite3"
    database = Database(path)
    database.initialize()
    path.unlink()
    with pytest.raises(MemoryRepositoryError, match="unavailable"):
        MemoryRepository(database).add(_candidate())
    assert not path.exists()
