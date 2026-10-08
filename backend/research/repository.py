"""Durable research sessions, sources, and claim-to-source citations.

The repository stores only what callers hand it and what the state machine
allows. It never fetches pages, never keeps page text (only a SHA-256 digest),
and never stores upstream error messages: failures use a fixed set of codes.
There is deliberately no physical deletion API.
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from backend.core.database import Database
from backend.research.models import (
    ALLOWED_TRANSITIONS,
    MAX_CLAIM_CHARS,
    MAX_PUBLISHER_CHARS,
    MAX_QUERY_CHARS,
    MAX_QUESTION_CHARS,
    MAX_QUOTE_CHARS,
    MAX_RESULT_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    TERMINAL_STATUSES,
    FailureReason,
    ResearchClaim,
    ResearchLevel,
    ResearchQueryRecord,
    ResearchSession,
    ResearchSource,
    ResearchStatus,
    SourceEvaluation,
    SourceType,
)

logger = logging.getLogger(__name__)

MAX_QUOTE_OFFSET = 10_000_000
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_EVALUATION_COLUMNS = (
    ("authority", "authority"),
    ("freshness", "freshness"),
    ("primary", "is_primary"),
    ("relevance", "relevance"),
    ("agreement", "agreement"),
)


class ResearchRepositoryError(RuntimeError):
    """Research data could not be stored or read."""


class ResearchSessionNotFound(ResearchRepositoryError):
    """The research session does not exist."""


class ResearchStateChanged(ResearchRepositoryError):
    """The session is terminal or its status changed since the caller observed it."""


class InvalidTransition(ResearchRepositoryError):
    """The requested status change is not in the allowed-transition table."""


class ResearchIntegrityError(ResearchRepositoryError):
    """A record would link data across sessions or reference missing data."""


class ResearchRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    # ----- sessions -----

    def create_session(
        self, question: str, level: ResearchLevel = ResearchLevel.QUICK
    ) -> ResearchSession:
        question = _bounded_text(question, "question", MAX_QUESTION_CHARS)
        if not isinstance(level, ResearchLevel):
            raise ValueError("level must be a ResearchLevel")
        now = self._now()
        session = ResearchSession(
            id=uuid4(),
            question=question,
            level=level,
            status=ResearchStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._write() as connection:
            connection.execute(
                "INSERT INTO research_sessions "
                "(id, question, level, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(session.id),
                    question,
                    level.value,
                    session.status.value,
                    _ts(now),
                    _ts(now),
                ),
            )
        return session

    def get_session(self, session_id: UUID) -> ResearchSession | None:
        _require_uuid(session_id, "session_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM research_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
        return _session(row) if row is not None else None

    def list_sessions(
        self, status: ResearchStatus | None = None, *, limit: int = 100
    ) -> list[ResearchSession]:
        if status is not None and not isinstance(status, ResearchStatus):
            raise ValueError("status must be a ResearchStatus")
        _require_limit(limit)
        query = "SELECT * FROM research_sessions"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY created_at, id LIMIT ?"
        params.append(limit)
        with self._read() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_session(row) for row in rows]

    def transition(
        self,
        session_id: UUID,
        expected: ResearchStatus,
        new: ResearchStatus,
        *,
        failure_reason: FailureReason | None = None,
    ) -> ResearchSession:
        """Compare-and-swap the status. Completion goes through set_result."""
        _require_uuid(session_id, "session_id")
        if not isinstance(expected, ResearchStatus) or not isinstance(new, ResearchStatus):
            raise ValueError("expected and new must be ResearchStatus values")
        if new not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransition(f"Cannot move research from {expected.value} to {new.value}")
        if new is ResearchStatus.COMPLETED:
            raise InvalidTransition("Completion requires a result; use set_result")
        if new is ResearchStatus.FAILED:
            if not isinstance(failure_reason, FailureReason):
                raise ValueError("Failure requires a FailureReason code")
        elif failure_reason is not None:
            raise ValueError("Only a failed session accepts a failure reason")
        with self._write() as connection:
            updated = connection.execute(
                "UPDATE research_sessions SET status = ?, failure_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    new.value,
                    failure_reason.value if failure_reason else None,
                    _ts(self._now()),
                    str(session_id),
                    expected.value,
                ),
            )
            if updated.rowcount != 1:
                self._raise_not_swapped(connection, session_id)
            row = _fetch_session_row(connection, session_id)
        return _session(row)

    def set_result(self, session_id: UUID, result_text: str) -> ResearchSession:
        """Complete a running session with its result in one atomic step."""
        _require_uuid(session_id, "session_id")
        result_text = _bounded_text(result_text, "result_text", MAX_RESULT_CHARS)
        with self._write() as connection:
            updated = connection.execute(
                "UPDATE research_sessions SET status = ?, result_text = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    ResearchStatus.COMPLETED.value,
                    result_text,
                    _ts(self._now()),
                    str(session_id),
                    ResearchStatus.RUNNING.value,
                ),
            )
            if updated.rowcount != 1:
                self._raise_not_swapped(connection, session_id)
            row = _fetch_session_row(connection, session_id)
        return _session(row)

    # ----- queries -----

    def add_query(self, session_id: UUID, text: str) -> ResearchQueryRecord:
        _require_uuid(session_id, "session_id")
        text = _bounded_text(text, "text", MAX_QUERY_CHARS)
        record_id = uuid4()
        now = self._now()
        with self._write() as connection:
            self._require_open_session(connection, session_id)
            position = connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM research_queries WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO research_queries (id, session_id, text, position, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(record_id), str(session_id), text, position, _ts(now)),
            )
        return ResearchQueryRecord(record_id, session_id, text, position, now)

    def list_queries(self, session_id: UUID) -> list[ResearchQueryRecord]:
        _require_uuid(session_id, "session_id")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM research_queries WHERE session_id = ? ORDER BY position",
                (str(session_id),),
            ).fetchall()
        return [
            ResearchQueryRecord(
                UUID(row["id"]),
                UUID(row["session_id"]),
                row["text"],
                row["position"],
                _parse_ts(row["created_at"]),
            )
            for row in rows
        ]

    # ----- sources -----

    def add_source(
        self,
        session_id: UUID,
        *,
        url: str,
        final_url: str,
        retrieved_at: datetime,
        content_digest: str,
        title: str | None = None,
        publisher: str | None = None,
        published_at: datetime | None = None,
        source_type: SourceType = SourceType.UNKNOWN,
        evaluation: SourceEvaluation | None = None,
    ) -> ResearchSource:
        """Store a source; the same (final_url, digest) in a session returns the first row."""
        _require_uuid(session_id, "session_id")
        url = _http_url(url, "url")
        final_url = _http_url(final_url, "final_url")
        if not isinstance(content_digest, str) or not _SHA256_HEX.fullmatch(content_digest):
            raise ValueError("content_digest must be a lowercase SHA-256 hex string")
        if not isinstance(source_type, SourceType):
            raise ValueError("source_type must be a SourceType")
        if evaluation is None:
            evaluation = SourceEvaluation()
        elif not isinstance(evaluation, SourceEvaluation):
            raise ValueError("evaluation must be a SourceEvaluation")
        title = _optional_text(title, "title", MAX_TITLE_CHARS)
        publisher = _optional_text(publisher, "publisher", MAX_PUBLISHER_CHARS)
        retrieved = _ts(retrieved_at)
        published = _ts(published_at) if published_at is not None else None
        source_id = uuid4()
        with self._write() as connection:
            self._require_open_session(connection, session_id)
            inserted = connection.execute(
                "INSERT INTO research_sources (id, session_id, url, final_url, title, "
                "publisher, published_at, retrieved_at, content_digest, source_type, "
                "authority, freshness, is_primary, relevance, agreement) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (session_id, final_url, content_digest) DO NOTHING",
                (
                    str(source_id),
                    str(session_id),
                    url,
                    final_url,
                    title,
                    publisher,
                    published,
                    retrieved,
                    content_digest,
                    source_type.value,
                    evaluation.authority,
                    evaluation.freshness,
                    evaluation.primary,
                    evaluation.relevance,
                    evaluation.agreement,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_sources "
                "WHERE session_id = ? AND final_url = ? AND content_digest = ?",
                (str(session_id), final_url, content_digest),
            ).fetchone()
        if inserted.rowcount == 0:
            logger.debug("research source already stored for session")
        return _source(row)

    def get_source(self, source_id: UUID) -> ResearchSource | None:
        _require_uuid(source_id, "source_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM research_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
        return _source(row) if row is not None else None

    def list_sources(self, session_id: UUID) -> list[ResearchSource]:
        _require_uuid(session_id, "session_id")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM research_sources WHERE session_id = ? ORDER BY retrieved_at, id",
                (str(session_id),),
            ).fetchall()
        return [_source(row) for row in rows]

    def set_evaluation(self, source_id: UUID, evaluation: SourceEvaluation) -> ResearchSource:
        """Replace a source's ratings (each 0..1 or None) while its session is open."""
        _require_uuid(source_id, "source_id")
        if not isinstance(evaluation, SourceEvaluation):
            raise ValueError("evaluation must be a SourceEvaluation")
        with self._write() as connection:
            owner = connection.execute(
                "SELECT session_id FROM research_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
            if owner is None:
                raise ResearchIntegrityError("Source does not exist")
            self._require_open_session(connection, UUID(owner["session_id"]))
            connection.execute(
                "UPDATE research_sources SET authority = ?, freshness = ?, is_primary = ?, "
                "relevance = ?, agreement = ? WHERE id = ?",
                (
                    evaluation.authority,
                    evaluation.freshness,
                    evaluation.primary,
                    evaluation.relevance,
                    evaluation.agreement,
                    str(source_id),
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
        return _source(row)

    # ----- claims / citations -----

    def add_claim(
        self,
        session_id: UUID,
        *,
        claim_text: str,
        source_id: UUID,
        quote: str,
        quote_start: int | None = None,
        quote_end: int | None = None,
    ) -> ResearchClaim:
        """Link a claim to a supporting source of the same session."""
        _require_uuid(session_id, "session_id")
        _require_uuid(source_id, "source_id")
        claim_text = _bounded_text(claim_text, "claim_text", MAX_CLAIM_CHARS)
        quote = _bounded_text(quote, "quote", MAX_QUOTE_CHARS)
        if (quote_start is None) != (quote_end is None):
            raise ValueError("quote_start and quote_end must be given together")
        if quote_start is not None:
            for value in (quote_start, quote_end):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("quote offsets must be integers")
            if not 0 <= quote_start < quote_end <= MAX_QUOTE_OFFSET:
                raise ValueError("quote offsets must satisfy 0 <= start < end within bounds")
        claim_id = uuid4()
        with self._write() as connection:
            self._require_open_session(connection, session_id)
            owner = connection.execute(
                "SELECT 1 FROM research_sources WHERE id = ? AND session_id = ?",
                (str(source_id), str(session_id)),
            ).fetchone()
            if owner is None:
                raise ResearchIntegrityError("Source does not belong to this research session")
            connection.execute(
                "INSERT INTO research_claims (id, session_id, claim_text, source_id, quote, "
                "quote_start, quote_end) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(claim_id),
                    str(session_id),
                    claim_text,
                    str(source_id),
                    quote,
                    quote_start,
                    quote_end,
                ),
            )
        return ResearchClaim(
            claim_id, session_id, claim_text, source_id, quote, quote_start, quote_end
        )

    def list_claims(self, session_id: UUID) -> list[ResearchClaim]:
        _require_uuid(session_id, "session_id")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM research_claims WHERE session_id = ? ORDER BY rowid",
                (str(session_id),),
            ).fetchall()
        return [_claim(row) for row in rows]

    def list_claims_for_source(self, source_id: UUID) -> list[ResearchClaim]:
        _require_uuid(source_id, "source_id")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM research_claims WHERE source_id = ? ORDER BY rowid",
                (str(source_id),),
            ).fetchall()
        return [_claim(row) for row in rows]

    # ----- internals -----

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _read(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._scope(write=False)

    def _write(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._scope(write=True)

    @contextmanager
    def _scope(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """Writers hold one immediate transaction; storage errors become repository errors."""
        try:
            with self.database.connect(read_only=not write) as connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    if write:
                        connection.rollback()
                    raise
                else:
                    if write:
                        connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ResearchIntegrityError("Research record violates storage constraints") from exc
        except (OSError, sqlite3.Error) as exc:
            raise ResearchRepositoryError("Research storage unavailable") from exc

    @staticmethod
    def _require_open_session(connection: sqlite3.Connection, session_id: UUID) -> None:
        row = connection.execute(
            "SELECT status FROM research_sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
        if row is None:
            raise ResearchSessionNotFound("Research session does not exist")
        if ResearchStatus(row["status"]) in TERMINAL_STATUSES:
            raise ResearchStateChanged("Research session is finished and cannot change")

    @staticmethod
    def _raise_not_swapped(connection: sqlite3.Connection, session_id: UUID) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM research_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
            is None
        ):
            raise ResearchSessionNotFound("Research session does not exist")
        raise ResearchStateChanged("Research session status changed")


def _ts(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ResearchRepositoryError("Stored research timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ResearchRepositoryError("Stored research timestamp is invalid")
    return parsed.astimezone(UTC)


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _require_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")


def _has_control(value: str) -> bool:
    return any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value)


def _bounded_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonblank text of at most {limit} characters")
    if _has_control(value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _optional_text(value: object, name: str, limit: int) -> str | None:
    return None if value is None else _bounded_text(value, name, limit)


def _http_url(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be an absolute http(s) URL")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError as exc:
        raise ValueError(f"{name} must be an absolute http(s) URL") from exc
    if parts.scheme not in ("http", "https") or not hostname:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return value


def _fetch_session_row(connection: sqlite3.Connection, session_id: UUID) -> sqlite3.Row:
    return connection.execute(
        "SELECT * FROM research_sessions WHERE id = ?", (str(session_id),)
    ).fetchone()


def _session(row: sqlite3.Row) -> ResearchSession:
    try:
        return ResearchSession(
            id=UUID(row["id"]),
            question=row["question"],
            level=ResearchLevel(row["level"]),
            status=ResearchStatus(row["status"]),
            created_at=_parse_ts(row["created_at"]),
            updated_at=_parse_ts(row["updated_at"]),
            result_text=row["result_text"],
            failure_reason=FailureReason(row["failure_reason"])
            if row["failure_reason"] is not None
            else None,
        )
    except ValueError as exc:
        raise ResearchRepositoryError("Stored research session is invalid") from exc


def _source(row: sqlite3.Row) -> ResearchSource:
    try:
        return ResearchSource(
            id=UUID(row["id"]),
            session_id=UUID(row["session_id"]),
            url=row["url"],
            final_url=row["final_url"],
            title=row["title"],
            publisher=row["publisher"],
            published_at=_parse_ts(row["published_at"]) if row["published_at"] else None,
            retrieved_at=_parse_ts(row["retrieved_at"]),
            content_digest=row["content_digest"],
            source_type=SourceType(row["source_type"]),
            evaluation=SourceEvaluation(
                **{field: row[column] for field, column in _EVALUATION_COLUMNS}
            ),
        )
    except ValueError as exc:
        raise ResearchRepositoryError("Stored research source is invalid") from exc


def _claim(row: sqlite3.Row) -> ResearchClaim:
    try:
        return ResearchClaim(
            id=UUID(row["id"]),
            session_id=UUID(row["session_id"]),
            claim_text=row["claim_text"],
            source_id=UUID(row["source_id"]),
            quote=row["quote"],
            quote_start=row["quote_start"],
            quote_end=row["quote_end"],
        )
    except ValueError as exc:
        raise ResearchRepositoryError("Stored research claim is invalid") from exc
