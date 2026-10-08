"""Schema v6 (research tables) migration from v5 and its safety refusals."""

import sqlite3
from pathlib import Path

import pytest

from backend.core.database import SCHEMA_VERSION, Database, DatabaseError

RESEARCH_TABLES = ("research_sessions", "research_queries", "research_sources", "research_claims")


def _drop_v8(connection: sqlite3.Connection) -> None:
    """Undo the additive v8 changes on a freshly migrated database (keeps user_version)."""
    connection.execute("DROP TABLE research_conflicts")
    connection.execute("DROP TABLE research_source_reasons")
    connection.execute("DROP INDEX research_claims_by_session_id")
    connection.execute("ALTER TABLE research_sources DROP COLUMN classification_rule")
    connection.execute("ALTER TABLE research_sources DROP COLUMN classification_basis")
    connection.execute("DELETE FROM schema_migrations WHERE version = 8")


def _downgrade_to_v5(path: Path) -> None:
    """Turn a freshly migrated database into a real v5 layout (no research tables)."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        _drop_v8(connection)
        for table in ("task_steps", "tasks"):
            connection.execute(f"DROP TABLE {table}")
        for table in ("research_claims", "research_sources", "research_queries"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DROP TABLE research_sessions")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (6, 7)")
        connection.execute("PRAGMA user_version = 5")


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_schema_version_is_eight_and_fresh_database_has_research_tables(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 8
    path = tmp_path / "fresh.sqlite3"
    Database(path).initialize()
    assert set(RESEARCH_TABLES) <= _tables(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        columns = [row[1] for row in connection.execute("PRAGMA table_info(research_queries)")]
    assert columns == ["id", "session_id", "text", "position", "created_at"]
    assert Database(path).is_ready()


def test_v5_database_upgrades_and_keeps_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "v5.sqlite3"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO conversations (id, created_at, updated_at) VALUES ('c1', 't', 't')"
        )
        connection.execute(
            "INSERT INTO conversation_messages (conversation_id, role, content, created_at) "
            "VALUES ('c1', 'user', 'hello', 't')"
        )
        connection.execute(
            "INSERT INTO memory_records (id, category, content, source, origin, importance, "
            "confidence, created_at, updated_at, tags, status) VALUES "
            "('m1', 'user', 'likes tea', 'conversation:1', 'user_explicit', 0.5, 1.0, "
            "'t', 't', '[]', 'approved')"
        )
    _downgrade_to_v5(path)
    assert not set(RESEARCH_TABLES) & _tables(path)
    assert not database.is_ready()

    database.initialize()

    assert database.is_ready()
    assert set(RESEARCH_TABLES) <= _tables(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert [r[0] for r in connection.execute("SELECT version FROM schema_migrations")] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
        ]
        assert connection.execute("SELECT content FROM conversation_messages").fetchall() == [
            ("hello",)
        ]
        assert connection.execute("SELECT content, status FROM memory_records").fetchall() == [
            ("likes tea", "approved")
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_rerun_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "again.sqlite3"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO research_sessions (id, question, level, status, created_at, updated_at) "
            "VALUES ('s1', 'q', 'quick', 'pending', 't', 't')"
        )
    database.initialize()
    database.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM research_sessions").fetchall() == [("s1",)]
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 8


def test_database_from_a_newer_schema_is_refused_untouched(tmp_path: Path) -> None:
    path = tmp_path / "v9.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (9, 't')")
        connection.execute("PRAGMA user_version = 9")
    with pytest.raises(DatabaseError, match="newer than supported"):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


def test_v5_with_broken_history_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite3"
    Database(path).initialize()
    _downgrade_to_v5(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    with pytest.raises(DatabaseError, match="migration history disagree"):
        Database(path).initialize()
    assert not set(RESEARCH_TABLES) & _tables(path)
