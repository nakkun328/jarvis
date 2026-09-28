from pathlib import Path

import pytest

from backend.core.database import Database


def test_database_initialization_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "nested" / "jarvis.sqlite3")
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
        assert [row[0] for row in versions] == [1]
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_connection_rolls_back_on_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "jarvis.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("CREATE TABLE temporary_data (value TEXT)")
    with pytest.raises(RuntimeError), database.connect() as connection:
        connection.execute("INSERT INTO temporary_data VALUES ('bad')")
        raise RuntimeError("failed operation")
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM temporary_data").fetchone()[0] == 0
        )
