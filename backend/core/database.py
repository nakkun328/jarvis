"""SQLite connection and schema bootstrap."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
_HISTORY_MISMATCH = "SQLite schema version and migration history disagree"


class DatabaseError(RuntimeError):
    """The SQLite database could not be prepared or queried."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        target = f"{self.path.resolve().as_uri()}?mode=ro" if read_only else self.path
        connection = sqlite3.connect(target, timeout=5, uri=read_only)
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
                        connection.execute(
                            "INSERT INTO schema_migrations (version, applied_at) "
                            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                            (SCHEMA_VERSION,),
                        )
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    else:
                        has_history = connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE type = 'table' AND name = 'schema_migrations'"
                        ).fetchone()
                        if has_history is None:
                            raise DatabaseError(_HISTORY_MISMATCH)
                        applied = connection.execute(
                            "SELECT 1 FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,)
                        ).fetchone()
                        if applied is None:
                            raise DatabaseError(_HISTORY_MISMATCH)
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
