"""Durable research sessions, sources, and claim-to-source citations.

The repository stores only what callers hand it and what the state machine
allows. It never fetches pages, never keeps page text (only a SHA-256 digest),
and never stores upstream error messages: failures use a fixed set of codes.
There is deliberately no physical deletion API.
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
    MAX_REASONS_PER_RATING,
    MAX_RESULT_CHARS,
    MAX_RULE_ID_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    RATING_REASONS,
    TERMINAL_STATUSES,
    Basis,
    ConflictKind,
    ConflictResolution,
    ConflictStatus,
    FailureReason,
    RatingName,
    RatingReason,
    RatingReasons,
    ResearchClaim,
    ResearchConflict,
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
_RULE_ID = re.compile(r"[a-z][a-z0-9_]*")
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
        self,
        status: ResearchStatus | None = None,
        *,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[ResearchSession]:
        """List sessions ordered by creation time, with the id as a stable tie-break.

        Oldest first by default; `newest_first` reverses both keys so LIMIT keeps the
        most recent sessions.
        """
        if status is not None and not isinstance(status, ResearchStatus):
            raise ValueError("status must be a ResearchStatus")
        _require_limit(limit)
        if not isinstance(newest_first, bool):
            raise ValueError("newest_first must be a bool")
        query = "SELECT * FROM research_sessions"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += (
            " ORDER BY created_at DESC, id DESC LIMIT ?"
            if newest_first
            else " ORDER BY created_at, id LIMIT ?"
        )
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

    def set_reuse_decision(
        self,
        session_id: UUID,
        reason: str,
        *,
        reuse_of: UUID | None = None,
        prior_at: datetime | None = None,
    ) -> ResearchSession:
        """Record the past-research reuse decision of an unfinished session.

        ``reason`` is one of the fixed codes of ``reuse.ReuseReason`` (the schema rejects any
        other). Only a session that is not yet in a terminal state accepts it.
        """
        _require_uuid(session_id, "session_id")
        if reuse_of is not None:
            _require_uuid(reuse_of, "reuse_of")
        with self._write() as connection:
            self._require_open_session(connection, session_id)
            try:
                connection.execute(
                    "UPDATE research_sessions SET reuse_reason = ?, reuse_of = ?, "
                    "reuse_prior_at = ? WHERE id = ?",
                    (
                        reason,
                        str(reuse_of) if reuse_of is not None else None,
                        _ts(prior_at) if prior_at is not None else None,
                        str(session_id),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError("unknown reuse reason or session") from None
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

    def count_queries_in_month(self, moment: datetime) -> int:
        """Search queries recorded during the UTC calendar month containing ``moment``.

        Read-only. This counts what JARVIS recorded, not what a vendor billed.
        """
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ValueError("moment must be a timezone-aware datetime")
        moment = moment.astimezone(UTC)
        start = datetime(moment.year, moment.month, 1, tzinfo=UTC)
        end = (
            datetime(moment.year + 1, 1, 1, tzinfo=UTC)
            if moment.month == 12
            else datetime(moment.year, moment.month + 1, 1, tzinfo=UTC)
        )
        with self._read() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM research_queries WHERE created_at >= ? AND created_at < ?",
                (_ts(start), _ts(end)),
            ).fetchone()
        return int(row[0])

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
            reasons = _load_reasons(connection, [row["id"]])
        if inserted.rowcount == 0:
            logger.debug("research source already stored for session")
        return _source(row, reasons)

    def get_source(self, source_id: UUID) -> ResearchSource | None:
        _require_uuid(source_id, "source_id")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM research_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
            reasons = _load_reasons(connection, [str(source_id)]) if row is not None else {}
        return _source(row, reasons) if row is not None else None

    def list_sources(self, session_id: UUID) -> list[ResearchSource]:
        _require_uuid(session_id, "session_id")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM research_sources WHERE session_id = ? ORDER BY retrieved_at, rowid",
                (str(session_id),),
            ).fetchall()
            reasons = _load_reasons(connection, [row["id"] for row in rows])
        return [_source(row, reasons) for row in rows]

    def set_evaluation(
        self,
        source_id: UUID,
        evaluation: SourceEvaluation,
        *,
        reasons: Mapping[RatingName, Sequence[RatingReason]] | None = None,
    ) -> ResearchSource:
        """Replace a source's ratings (each 0..1 or None) while its session is open.

        ``reasons`` optionally records the fixed reason codes of ratings: for every rating
        named in the mapping the stored codes are replaced (an empty sequence clears them);
        ratings not named keep their codes. Each code must belong to its rating.
        """
        return self._update_source(source_id, evaluation=evaluation, reasons=reasons)

    def set_source_classification(
        self,
        source_id: UUID,
        source_type: SourceType,
        *,
        rule_id: str,
        basis: Basis,
    ) -> ResearchSource:
        """Record a source type with the rule id and basis that decided it (session open).

        ``rule_id`` is a short identifier from a rule table (``official_host``, ``no_rule``,
        ``provided``), never page text. Calling it again with the same values changes nothing.
        """
        return self._update_source(source_id, classification=(source_type, rule_id, basis))

    def set_source_assessment(
        self,
        source_id: UUID,
        *,
        source_type: SourceType,
        rule_id: str,
        basis: Basis,
        evaluation: SourceEvaluation,
        reasons: Mapping[RatingName, Sequence[RatingReason]] | None = None,
    ) -> ResearchSource:
        """``set_source_classification`` and ``set_evaluation`` in one atomic write."""
        return self._update_source(
            source_id,
            classification=(source_type, rule_id, basis),
            evaluation=evaluation,
            reasons=reasons,
        )

    def _update_source(
        self,
        source_id: UUID,
        *,
        classification: tuple[SourceType, str, Basis] | None = None,
        evaluation: SourceEvaluation | None = None,
        reasons: Mapping[RatingName, Sequence[RatingReason]] | None = None,
    ) -> ResearchSource:
        _require_uuid(source_id, "source_id")
        if classification is not None:
            source_type, rule_id, basis = classification
            if not isinstance(source_type, SourceType):
                raise ValueError("source_type must be a SourceType")
            if not isinstance(basis, Basis):
                raise ValueError("basis must be a Basis")
            if (
                not isinstance(rule_id, str)
                or len(rule_id) > MAX_RULE_ID_CHARS
                or not _RULE_ID.fullmatch(rule_id)
            ):
                raise ValueError("rule_id must be a short lowercase identifier")
        if evaluation is not None and not isinstance(evaluation, SourceEvaluation):
            raise ValueError("evaluation must be a SourceEvaluation")
        checked = _check_reasons(reasons)
        with self._write() as connection:
            owner = connection.execute(
                "SELECT session_id FROM research_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
            if owner is None:
                raise ResearchIntegrityError("Source does not exist")
            self._require_open_session(connection, UUID(owner["session_id"]))
            if classification is not None:
                connection.execute(
                    "UPDATE research_sources SET source_type = ?, classification_rule = ?, "
                    "classification_basis = ? WHERE id = ?",
                    (source_type.value, rule_id, basis.value, str(source_id)),
                )
            if evaluation is not None:
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
            for rating, codes in checked.items():
                connection.execute(
                    "DELETE FROM research_source_reasons WHERE source_id = ? AND rating = ?",
                    (str(source_id), rating.value),
                )
                connection.executemany(
                    "INSERT INTO research_source_reasons (source_id, rating, position, reason) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (str(source_id), rating.value, position, code.value)
                        for position, code in enumerate(codes)
                    ],
                )
            row = connection.execute(
                "SELECT * FROM research_sources WHERE id = ?", (str(source_id),)
            ).fetchone()
            loaded = _load_reasons(connection, [str(source_id)])
        return _source(row, loaded)

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

    # ----- conflicts -----

    def add_conflict(
        self,
        session_id: UUID,
        kind: ConflictKind,
        claim_id: UUID,
        *,
        other_claim_id: UUID | None = None,
        other_source_id: UUID | None = None,
    ) -> ResearchConflict:
        """Record a disagreement; it stays open until ``resolve_conflict`` is called.

        Side A is ``claim_id`` and the source it cites. Side B is exactly one of
        ``other_claim_id`` (a second claim, its cited source becomes side B's source) or
        ``other_source_id`` (a source whose text disagrees with claim A). The two sides must
        cite different sources. Between two claims the pair is stored in a canonical order,
        so recording it again in either order returns the existing record.
        """
        _require_uuid(session_id, "session_id")
        _require_uuid(claim_id, "claim_id")
        if not isinstance(kind, ConflictKind):
            raise ValueError("kind must be a ConflictKind")
        if (other_claim_id is None) == (other_source_id is None):
            raise ValueError("give exactly one of other_claim_id and other_source_id")
        if other_claim_id is not None:
            _require_uuid(other_claim_id, "other_claim_id")
        if other_source_id is not None:
            _require_uuid(other_source_id, "other_source_id")
        with self._write() as connection:
            self._require_open_session(connection, session_id)
            first = _claim_side(connection, session_id, claim_id)
            if other_claim_id is not None:
                second = _claim_side(connection, session_id, other_claim_id)
                if first[0] == second[0]:
                    raise ValueError("a conflict needs two different claims")
                if str(first[0]) > str(second[0]):
                    first, second = second, first
                claim_a, source_a = first
                claim_b, source_b = second
            else:
                claim_a, source_a = first
                claim_b = None
                owner = connection.execute(
                    "SELECT 1 FROM research_sources WHERE id = ? AND session_id = ?",
                    (str(other_source_id), str(session_id)),
                ).fetchone()
                if owner is None:
                    raise ResearchIntegrityError("Source does not belong to this research session")
                source_b = other_source_id
            if source_a == source_b:
                raise ValueError("a conflict needs two different sources")
            connection.execute(
                "INSERT INTO research_conflicts (id, session_id, kind, claim_a_id, source_a_id, "
                "claim_b_id, source_b_id, status, resolution, detected_at, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, NULL) "
                "ON CONFLICT DO NOTHING",
                (
                    str(uuid4()),
                    str(session_id),
                    kind.value,
                    str(claim_a),
                    str(source_a),
                    str(claim_b) if claim_b is not None else None,
                    str(source_b),
                    _ts(self._now()),
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_conflicts WHERE claim_a_id = ? "
                "AND COALESCE(claim_b_id, '') = ? AND source_b_id = ? AND kind = ?",
                (
                    str(claim_a),
                    str(claim_b) if claim_b is not None else "",
                    str(source_b),
                    kind.value,
                ),
            ).fetchone()
        return _conflict(row)

    def list_conflicts(
        self, session_id: UUID, *, status: ConflictStatus | None = None
    ) -> list[ResearchConflict]:
        _require_uuid(session_id, "session_id")
        if status is not None and not isinstance(status, ConflictStatus):
            raise ValueError("status must be a ConflictStatus")
        query = "SELECT * FROM research_conflicts WHERE session_id = ?"
        params: list[object] = [str(session_id)]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY detected_at, id"
        with self._read() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_conflict(row) for row in rows]

    def resolve_conflict(
        self, conflict_id: UUID, resolution: ConflictResolution
    ) -> ResearchConflict:
        """Close an open conflict with a fixed resolution code (explicit, never automatic)."""
        _require_uuid(conflict_id, "conflict_id")
        if not isinstance(resolution, ConflictResolution):
            raise ValueError("resolution must be a ConflictResolution")
        with self._write() as connection:
            owner = connection.execute(
                "SELECT session_id, status FROM research_conflicts WHERE id = ?",
                (str(conflict_id),),
            ).fetchone()
            if owner is None:
                raise ResearchIntegrityError("Conflict does not exist")
            self._require_open_session(connection, UUID(owner["session_id"]))
            if owner["status"] != ConflictStatus.OPEN.value:
                raise ResearchStateChanged("Conflict is already resolved")
            connection.execute(
                "UPDATE research_conflicts SET status = 'resolved', resolution = ?, "
                "resolved_at = ? WHERE id = ?",
                (resolution.value, _ts(self._now()), str(conflict_id)),
            )
            row = connection.execute(
                "SELECT * FROM research_conflicts WHERE id = ?", (str(conflict_id),)
            ).fetchone()
        return _conflict(row)

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
            reuse_reason=row["reuse_reason"],
            reuse_of=UUID(row["reuse_of"]) if row["reuse_of"] is not None else None,
            reuse_prior_at=_parse_ts(row["reuse_prior_at"])
            if row["reuse_prior_at"] is not None
            else None,
        )
    except ValueError as exc:
        raise ResearchRepositoryError("Stored research session is invalid") from exc


def _check_reasons(
    reasons: Mapping[RatingName, Sequence[RatingReason]] | None,
) -> dict[RatingName, tuple[RatingReason, ...]]:
    if reasons is None:
        return {}
    if not isinstance(reasons, Mapping):
        raise ValueError("reasons must be a mapping of rating to reason codes")
    checked: dict[RatingName, tuple[RatingReason, ...]] = {}
    for rating, codes in reasons.items():
        if not isinstance(rating, RatingName):
            raise ValueError("reasons keys must be RatingName values")
        if isinstance(codes, str) or not isinstance(codes, Sequence):
            raise ValueError("reason codes must be a sequence")
        if len(codes) > MAX_REASONS_PER_RATING:
            raise ValueError(f"at most {MAX_REASONS_PER_RATING} reason codes per rating")
        if not all(isinstance(code, RatingReason) for code in codes):
            raise ValueError("reason codes must be RatingReason values")
        if len(set(codes)) != len(codes):
            raise ValueError("reason codes must not repeat")
        if not set(codes) <= RATING_REASONS[rating]:
            raise ValueError("a reason code does not belong to its rating")
        checked[rating] = tuple(codes)
    return checked


def _load_reasons(
    connection: sqlite3.Connection, source_ids: Iterable[str]
) -> dict[str, dict[str, list[str]]]:
    """Reason codes by source id then rating, in recorded order."""
    ids = list(source_ids)
    found: dict[str, dict[str, list[str]]] = {}
    # Chunked so a long list never exceeds SQLite's variable limit.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = connection.execute(
            "SELECT source_id, rating, reason FROM research_source_reasons "
            f"WHERE source_id IN ({marks}) ORDER BY source_id, rating, position",
            chunk,
        ).fetchall()
        for row in rows:
            found.setdefault(row["source_id"], {}).setdefault(row["rating"], []).append(
                row["reason"]
            )
    return found


def _claim_side(
    connection: sqlite3.Connection, session_id: UUID, claim_id: UUID
) -> tuple[UUID, UUID]:
    row = connection.execute(
        "SELECT source_id FROM research_claims WHERE id = ? AND session_id = ?",
        (str(claim_id), str(session_id)),
    ).fetchone()
    if row is None:
        raise ResearchIntegrityError("Claim does not belong to this research session")
    return claim_id, UUID(row["source_id"])


def _conflict(row: sqlite3.Row) -> ResearchConflict:
    try:
        return ResearchConflict(
            id=UUID(row["id"]),
            session_id=UUID(row["session_id"]),
            kind=ConflictKind(row["kind"]),
            claim_a_id=UUID(row["claim_a_id"]),
            source_a_id=UUID(row["source_a_id"]),
            source_b_id=UUID(row["source_b_id"]),
            detected_at=_parse_ts(row["detected_at"]),
            claim_b_id=UUID(row["claim_b_id"]) if row["claim_b_id"] else None,
            status=ConflictStatus(row["status"]),
            resolution=ConflictResolution(row["resolution"]) if row["resolution"] else None,
            resolved_at=_parse_ts(row["resolved_at"]) if row["resolved_at"] else None,
        )
    except ValueError as exc:
        raise ResearchRepositoryError("Stored research conflict is invalid") from exc


def _source(row: sqlite3.Row, reasons: Mapping[str, Mapping[str, list[str]]]) -> ResearchSource:
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
            classification_rule=row["classification_rule"],
            classification_basis=Basis(row["classification_basis"])
            if row["classification_basis"] is not None
            else None,
            reasons=RatingReasons(
                **{
                    name.value: tuple(
                        RatingReason(code)
                        for code in reasons.get(row["id"], {}).get(name.value, ())
                    )
                    for name in RatingName
                }
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
