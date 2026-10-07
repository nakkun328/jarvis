"""SQLite connection and schema bootstrap."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 6
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
                # v5 rebuilds a CHECK-constrained parent table. Validate all
                # references before commit, then close this migration connection.
                connection.execute("PRAGMA foreign_keys = OFF")
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
                    if version < 4:
                        connection.execute(
                            "CREATE TABLE memory_review_events ("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "memory_id TEXT NOT NULL REFERENCES memory_records(id), "
                            "previous_status TEXT NOT NULL CHECK(previous_status IN "
                            "('pending', 'conflict')), "
                            "new_status TEXT NOT NULL CHECK(new_status IN "
                            "('conflict', 'approved', 'rejected')), "
                            "action TEXT NOT NULL CHECK(action IN "
                            "('flag_conflict', 'approve', 'reject')), "
                            "actor TEXT NOT NULL, "
                            "occurred_at TEXT NOT NULL, vault_revision TEXT)"
                        )
                        connection.execute(
                            "CREATE INDEX memory_review_events_by_memory "
                            "ON memory_review_events(memory_id, id)"
                        )
                        self._record_migration(connection, 4)
                        connection.execute("PRAGMA user_version = 4")
                    if version < 5:
                        connection.execute(
                            "CREATE TABLE memory_records_v5 ("
                            "id TEXT PRIMARY KEY, category TEXT NOT NULL, "
                            "content TEXT NOT NULL, source TEXT NOT NULL, origin TEXT NOT NULL, "
                            "importance REAL NOT NULL, confidence REAL NOT NULL, "
                            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                            "last_accessed TEXT, tags TEXT NOT NULL, project TEXT, "
                            "status TEXT NOT NULL CHECK(status IN "
                            "('pending', 'conflict', 'approved', 'rejected', "
                            "'superseded', 'retired')), vault_revision TEXT, "
                            "supersedes_id TEXT REFERENCES memory_records(id), "
                            "supersedes_revision TEXT, "
                            "replaced_by_id TEXT REFERENCES memory_records(id), "
                            "CHECK ((supersedes_id IS NULL) = (supersedes_revision IS NULL)), "
                            "CHECK (status != 'superseded' OR replaced_by_id IS NOT NULL))"
                        )
                        connection.execute(
                            "INSERT INTO memory_records_v5 ("
                            "id, category, content, source, origin, importance, confidence, "
                            "created_at, updated_at, last_accessed, tags, project, status, "
                            "vault_revision) SELECT id, category, content, source, origin, "
                            "importance, confidence, created_at, updated_at, last_accessed, "
                            "tags, project, status, vault_revision FROM memory_records"
                        )
                        connection.execute("DROP TABLE memory_records")
                        connection.execute("ALTER TABLE memory_records_v5 RENAME TO memory_records")
                        connection.execute(
                            "CREATE INDEX memory_records_by_status "
                            "ON memory_records(status, created_at)"
                        )
                        connection.execute(
                            "CREATE INDEX memory_records_by_supersedes "
                            "ON memory_records(supersedes_id)"
                        )
                        connection.execute(
                            "CREATE TABLE memory_lifecycle_events ("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "memory_id TEXT NOT NULL REFERENCES memory_records(id), "
                            "related_id TEXT REFERENCES memory_records(id), "
                            "action TEXT NOT NULL CHECK(action IN ('supersede', 'retire')), "
                            "actor TEXT NOT NULL, reason TEXT NOT NULL, "
                            "occurred_at TEXT NOT NULL, vault_revision TEXT NOT NULL)"
                        )
                        connection.execute(
                            "CREATE INDEX memory_lifecycle_events_by_memory "
                            "ON memory_lifecycle_events(memory_id, id)"
                        )
                        self._record_migration(connection, 5)
                        connection.execute("PRAGMA user_version = 5")
                    if version < 6:
                        self._create_research_tables(connection)
                        self._record_migration(connection, 6)
                        connection.execute("PRAGMA user_version = 6")
                    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                        raise DatabaseError("SQLite foreign key check failed during migration")
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"Could not initialize SQLite database at {self.path}") from exc

    @staticmethod
    def _create_research_tables(connection: sqlite3.Connection) -> None:
        """v6: research sessions, planned queries, sources, and cited claims."""
        connection.execute(
            "CREATE TABLE research_sessions ("
            "id TEXT PRIMARY KEY, "
            "question TEXT NOT NULL CHECK(length(question) BETWEEN 1 AND 2000), "
            "level TEXT NOT NULL CHECK(level IN "
            "('memory', 'quick', 'standard', 'deep', 'extensive')), "
            "status TEXT NOT NULL CHECK(status IN "
            "('pending', 'running', 'waiting', 'failed', 'completed', 'cancelled')), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "result_text TEXT CHECK(result_text IS NULL OR length(result_text) <= 50000), "
            "failure_reason TEXT CHECK(failure_reason IS NULL OR failure_reason IN "
            "('search_failed', 'no_results', 'reader_failed', 'synthesis_failed', "
            "'timeout', 'budget_exceeded', 'internal_error')), "
            "CHECK (result_text IS NULL OR status = 'completed'), "
            "CHECK ((failure_reason IS NOT NULL) = (status = 'failed')))"
        )
        connection.execute(
            "CREATE INDEX research_sessions_by_status ON research_sessions(status, created_at)"
        )
        connection.execute(
            "CREATE TABLE research_queries ("
            "id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE, "
            "text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 500), "
            "position INTEGER NOT NULL CHECK(position >= 0), "
            "created_at TEXT NOT NULL, "
            "UNIQUE (session_id, position))"
        )
        connection.execute(
            "CREATE TABLE research_sources ("
            "id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE, "
            "url TEXT NOT NULL CHECK(length(url) BETWEEN 1 AND 2048), "
            "final_url TEXT NOT NULL CHECK(length(final_url) BETWEEN 1 AND 2048), "
            "title TEXT, publisher TEXT, published_at TEXT, retrieved_at TEXT NOT NULL, "
            "content_digest TEXT NOT NULL CHECK(length(content_digest) = 64), "
            "source_type TEXT NOT NULL CHECK(source_type IN "
            "('official', 'docs', 'academic', 'news', 'community', 'blog', 'forum', "
            "'unknown')), "
            "authority REAL CHECK(authority IS NULL OR authority BETWEEN 0 AND 1), "
            "freshness REAL CHECK(freshness IS NULL OR freshness BETWEEN 0 AND 1), "
            "is_primary REAL CHECK(is_primary IS NULL OR is_primary BETWEEN 0 AND 1), "
            "relevance REAL CHECK(relevance IS NULL OR relevance BETWEEN 0 AND 1), "
            "agreement REAL CHECK(agreement IS NULL OR agreement BETWEEN 0 AND 1), "
            "UNIQUE (session_id, final_url, content_digest), "
            "UNIQUE (session_id, id))"
        )
        connection.execute(
            "CREATE TABLE research_claims ("
            "id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE, "
            "claim_text TEXT NOT NULL CHECK(length(claim_text) BETWEEN 1 AND 2000), "
            "source_id TEXT NOT NULL, "
            "quote TEXT NOT NULL CHECK(length(quote) BETWEEN 1 AND 500), "
            "quote_start INTEGER CHECK(quote_start IS NULL OR quote_start >= 0), "
            "quote_end INTEGER CHECK(quote_end IS NULL OR quote_end >= 0), "
            "CHECK ((quote_start IS NULL) = (quote_end IS NULL)), "
            "CHECK (quote_start IS NULL OR quote_start < quote_end), "
            "FOREIGN KEY (session_id, source_id) "
            "REFERENCES research_sources(session_id, id) ON DELETE CASCADE)"
        )
        connection.execute("CREATE INDEX research_claims_by_session ON research_claims(session_id)")
        connection.execute("CREATE INDEX research_claims_by_source ON research_claims(source_id)")

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
