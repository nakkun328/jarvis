"""Schema v7 (task tables) migration from v6 and its safety refusals."""

import sqlite3
from pathlib import Path

import pytest

from backend.core.database import SCHEMA_VERSION, Database, DatabaseError

TASK_TABLES = ("tasks", "task_steps")


def _drop_v8(connection: sqlite3.Connection) -> None:
    """Undo the additive v8, v9 and v10 changes on a freshly migrated database."""
    connection.execute("ALTER TABLE research_sessions DROP COLUMN reuse_prior_at")
    connection.execute("ALTER TABLE research_sessions DROP COLUMN reuse_of")
    connection.execute("ALTER TABLE research_sessions DROP COLUMN reuse_reason")
    connection.execute("DELETE FROM schema_migrations WHERE version = 10")
    connection.execute("DROP TABLE tool_approvals")
    connection.execute("DELETE FROM schema_migrations WHERE version = 9")
    connection.execute("DROP TABLE research_conflicts")
    connection.execute("DROP TABLE research_source_reasons")
    connection.execute("DROP INDEX research_claims_by_session_id")
    connection.execute("ALTER TABLE research_sources DROP COLUMN classification_rule")
    connection.execute("ALTER TABLE research_sources DROP COLUMN classification_basis")
    connection.execute("DELETE FROM schema_migrations WHERE version = 8")


def _downgrade_to_v6(path: Path) -> None:
    """Turn a freshly migrated database into a real v6 layout (no task tables)."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        _drop_v8(connection)
        connection.execute("DROP TABLE task_steps")
        connection.execute("DROP TABLE tasks")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute("PRAGMA user_version = 6")


def _objects(path: Path, kind: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
        }


def test_fresh_database_is_current_with_task_tables_and_guards(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    database = Database(path)
    database.initialize()
    assert SCHEMA_VERSION == 10
    assert set(TASK_TABLES) <= _objects(path, "table")
    assert {"tasks_by_status", "tasks_by_retry_of"} <= _objects(path, "index")
    assert "tasks_terminal_is_immutable" in _objects(path, "trigger")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        columns = [row[1] for row in connection.execute("PRAGMA table_info(tasks)")]
    assert columns[:4] == ["id", "goal", "target_device", "status"]
    assert database.is_ready()


def test_v6_database_upgrades_and_keeps_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "v6.sqlite3"
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
        connection.execute(
            "INSERT INTO research_sessions (id, question, level, status, created_at, updated_at) "
            "VALUES ('r1', 'q', 'quick', 'pending', 't', 't')"
        )
    _downgrade_to_v6(path)
    assert not set(TASK_TABLES) & _objects(path, "table")
    assert not database.is_ready()

    database.initialize()

    assert database.is_ready()
    assert set(TASK_TABLES) <= _objects(path, "table")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert [r[0] for r in connection.execute("SELECT version FROM schema_migrations")] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
        ]
        assert connection.execute("SELECT content FROM conversation_messages").fetchall() == [
            ("hello",)
        ]
        assert connection.execute("SELECT content, status FROM memory_records").fetchall() == [
            ("likes tea", "approved")
        ]
        assert connection.execute("SELECT id FROM research_sessions").fetchall() == [("r1",)]
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_rerun_is_idempotent_and_keeps_tasks(tmp_path: Path) -> None:
    path = tmp_path / "again.sqlite3"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tasks (id, goal, status, created_at, updated_at) "
            "VALUES ('t1', 'goal', 'pending', 't', 't')"
        )
    database.initialize()
    database.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM tasks").fetchall() == [("t1",)]
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 10


def test_database_from_a_newer_schema_is_refused_untouched(tmp_path: Path) -> None:
    path = tmp_path / "v10.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (11, 't')")
        connection.execute("PRAGMA user_version = 11")
    with pytest.raises(DatabaseError, match="newer than supported"):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11


def test_v6_with_broken_history_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite3"
    Database(path).initialize()
    _downgrade_to_v6(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    with pytest.raises(DatabaseError, match="migration history disagree"):
        Database(path).initialize()
    assert not set(TASK_TABLES) & _objects(path, "table")


def test_failed_v7_migration_rolls_back_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "failed-v7.sqlite3"
    Database(path).initialize()
    _downgrade_to_v6(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_v7 BEFORE INSERT ON schema_migrations "
            "WHEN NEW.version = 7 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
    with pytest.raises(DatabaseError, match="Could not initialize"):
        Database(path).initialize()
    assert not set(TASK_TABLES) & _objects(path, "table")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_schema_rejects_inconsistent_task_rows(tmp_path: Path) -> None:
    path = tmp_path / "checks.sqlite3"
    Database(path).initialize()
    insert = (
        "INSERT INTO tasks (id, goal, status, created_at, updated_at, started_at, finished_at, "
        "result_summary, failure_code, waiting_reason, verified) "
        "VALUES (?, 'g', ?, 't', 't', ?, ?, ?, ?, ?, ?)"
    )
    bad_rows = [
        # completed without verification
        ("a", "completed", "t", "t", "done", None, None, "not_verified"),
        # completed with a failed verification
        ("b", "completed", "t", "t", "done", None, None, "verification_failed"),
        # failed without a code
        ("c", "failed", "t", "t", None, None, None, "not_verified"),
        # waiting without a reason
        ("d", "waiting", "t", None, None, None, None, "not_verified"),
        # running but already finished
        ("e", "running", "t", "t", None, None, None, "not_verified"),
        # unknown failure code
        ("f", "failed", "t", "t", None, "disk on fire", None, "not_verified"),
    ]
    with sqlite3.connect(path) as connection:
        for row in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, row)
