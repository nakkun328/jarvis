"""Foundation integration and failure-path tests."""

import os
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import ConfigError, Settings
from backend.core.database import SCHEMA_VERSION, Database, DatabaseError


def test_app_bootstraps_database_and_reports_ready(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "jarvis.sqlite3"
    with TestClient(create_app(Settings(db_path=db_path))) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert client.get("/docs").status_code == 404

    assert db_path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT version FROM schema_migrations").fetchone()[0] == 1


def test_readiness_detects_database_removal(tmp_path: Path) -> None:
    db_path = tmp_path / "jarvis.sqlite3"
    with TestClient(create_app(Settings(db_path=db_path))) as client:
        db_path.unlink()
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert not db_path.exists()


def test_rejects_database_from_future_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(DatabaseError, match="newer than supported"):
        Database(db_path).initialize()


def test_rejects_inconsistent_schema_history(tmp_path: Path) -> None:
    db_path = tmp_path / "broken.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    with pytest.raises(DatabaseError, match="schema version and migration history disagree"):
        Database(db_path).initialize()


def test_rejects_nonempty_unversioned_database_without_changing_it(tmp_path: Path) -> None:
    db_path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE existing_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO existing_data (value) VALUES ('keep me')")

    with pytest.raises(DatabaseError, match="Unversioned SQLite database is not empty"):
        Database(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT value FROM existing_data").fetchone()[0] == "keep me"
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'"
        ).fetchone() is None


def test_invalid_environment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_DB_PATH", " ")
    with pytest.raises(ConfigError, match="JARVIS_DB_PATH"):
        Settings.from_env()
    monkeypatch.setenv("JARVIS_DB_PATH", "test.sqlite3")
    monkeypatch.setenv("JARVIS_LOG_LEVEL", "verbose")
    with pytest.raises(ConfigError, match="JARVIS_LOG_LEVEL"):
        Settings.from_env()
