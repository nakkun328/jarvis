"""SQLite connection and schema bootstrap."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
_HISTORY_MISMATCH = "SQLite schema version and migration history disagree"


class DatabaseError(RuntimeError):
    """The SQLite database could not be prepared or queried."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        mode = "ro" if read_only else "rw"
        target = f"{self.path.resolve().as_uri()}?mode={mode}"
        connection = sqlite3.connect(target, timeout=5, uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            with self.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version > SCHEMA_VERSION:
                        raise DatabaseError(
                            f"Database schema {version} is newer than supported schema "
                            f"{SCHEMA_VERSION}"
                        )
                    if version == 0:
                        existing_object = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
                        ).fetchone()
                        if existing_object is not None:
                            raise DatabaseError(
                                "Unversioned SQLite database is not empty; refusing to claim it"
                            )
                        connection.execute(
                            "CREATE TABLE schema_migrations "
                            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                        )
                        self._record_migration(connection, 1)
                        connection.execute("PRAGMA user_version = 1")
                        version = 1
                    has_history = connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'schema_migrations'"
                    ).fetchone()
                    if has_history is None:
                        raise DatabaseError(_HISTORY_MISMATCH)
                    history = [
                        row[0]
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ]
                    if history != list(range(1, version + 1)):
                        raise DatabaseError(_HISTORY_MISMATCH)
                    if version < 2:
                        connection.execute(
                            "CREATE TABLE conversations ("
                            "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                            "updated_at TEXT NOT NULL)"
                        )
                        connection.execute(
                            "CREATE TABLE conversation_messages ("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "conversation_id TEXT NOT NULL REFERENCES conversations(id) "
                            "ON DELETE CASCADE, "
                            "role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), "
                            "content TEXT NOT NULL, created_at TEXT NOT NULL)"
                        )
                        connection.execute(
                            "CREATE INDEX conversation_messages_by_conversation "
                            "ON conversation_messages(conversation_id, id)"
                        )
                        self._record_migration(connection, 2)
                        connection.execute("PRAGMA user_version = 2")
                    if version < 3:
                        connection.execute(
                            "CREATE TABLE memory_records ("
                            "id TEXT PRIMARY KEY, category TEXT NOT NULL, "
                            "content TEXT NOT NULL, source TEXT NOT NULL, origin TEXT NOT NULL, "
                            "importance REAL NOT NULL, confidence REAL NOT NULL, "
                            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                            "last_accessed TEXT, tags TEXT NOT NULL, project TEXT, "
                            "status TEXT NOT NULL CHECK(status IN "
                            "('pending', 'conflict', 'approved', 'rejected')), "
                            "vault_revision TEXT)"
                        )
                        connection.execute(
                            "CREATE INDEX memory_records_by_status "
                            "ON memory_records(status, created_at)"
                        )
                        self._record_migration(connection, 3)
                        connection.execute("PRAGMA user_version = 3")
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"Could not initialize SQLite database at {self.path}") from exc

    def is_ready(self) -> bool:
        try:
            with self.connect(read_only=True) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,)
                ).fetchone()
                return version == SCHEMA_VERSION and applied is not None
        except sqlite3.Error:
            return False

    @staticmethod
    def _record_migration(connection: sqlite3.Connection, version: int) -> None:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (version,),
        )
