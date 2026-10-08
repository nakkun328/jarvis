"""Research sessions, sources, and claim citations in SQLite."""

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from backend.core.database import Database
from backend.research.models import (
    ALLOWED_TRANSITIONS,
    FailureReason,
    ResearchLevel,
    ResearchStatus,
    SourceEvaluation,
    SourceType,
)
from backend.research.repository import (
    InvalidTransition,
    ResearchIntegrityError,
    ResearchRepository,
    ResearchRepositoryError,
    ResearchSessionNotFound,
    ResearchStateChanged,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
DIGEST = hashlib.sha256(b"page bytes").hexdigest()


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "research.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def repository(database: Database) -> ResearchRepository:
    return ResearchRepository(database, clock=lambda: NOW)


def _source(repository: ResearchRepository, session_id, **overrides):
    values = {
        "url": "https://example.test/a",
        "final_url": "https://example.test/a",
        "retrieved_at": NOW,
        "content_digest": DIGEST,
        "title": "Example",
    }
    values.update(overrides)
    return repository.add_source(session_id, **values)


def _status_session(repository: ResearchRepository, status: ResearchStatus):
    session = repository.create_session("question")
    path = {
        ResearchStatus.PENDING: [],
        ResearchStatus.RUNNING: [ResearchStatus.RUNNING],
        ResearchStatus.WAITING: [ResearchStatus.RUNNING, ResearchStatus.WAITING],
        ResearchStatus.FAILED: [ResearchStatus.RUNNING, ResearchStatus.FAILED],
        ResearchStatus.CANCELLED: [ResearchStatus.CANCELLED],
    }
    current = ResearchStatus.PENDING
    for step in path.get(status, []):
        reason = FailureReason.TIMEOUT if step is ResearchStatus.FAILED else None
        session = repository.transition(session.id, current, step, failure_reason=reason)
        current = step
    if status is ResearchStatus.COMPLETED:
        repository.transition(session.id, current, ResearchStatus.RUNNING)
        session = repository.set_result(session.id, "done")
    return session


def test_create_session_defaults_and_persists(repository: ResearchRepository, database) -> None:
    session = repository.create_session("What changed in X?")
    assert session.level is ResearchLevel.QUICK
    assert session.status is ResearchStatus.PENDING
    assert session.created_at == session.updated_at == NOW
    assert session.result_text is None and session.failure_reason is None
    assert ResearchRepository(database).get_session(session.id) == session
    assert repository.get_session(uuid4()) is None
    assert repository.list_sessions() == [session]
    assert repository.list_sessions(ResearchStatus.RUNNING) == []


def _ticking(database: Database) -> ResearchRepository:
    """A repository whose clock moves forward one second on every call."""
    state = {"now": NOW}

    def clock() -> datetime:
        state["now"] += timedelta(seconds=1)
        return state["now"]

    return ResearchRepository(database, clock=clock)


def test_list_sessions_orders_oldest_first_by_default_and_newest_first_on_request(
    database: Database,
) -> None:
    repository = _ticking(database)
    first, second, third = (repository.create_session(f"q{n}") for n in range(3))
    assert repository.list_sessions() == [first, second, third]
    assert repository.list_sessions(newest_first=False) == [first, second, third]
    assert repository.list_sessions(newest_first=True) == [third, second, first]
    assert repository.list_sessions(limit=2) == [first, second]
    assert repository.list_sessions(limit=2, newest_first=True) == [third, second]


def test_list_sessions_newest_first_applies_status_filter_before_limit(
    database: Database,
) -> None:
    repository = _ticking(database)
    sessions = [repository.create_session(f"q{n}") for n in range(8)]
    running = []
    for session in sessions[::2]:
        repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
        running.append(session.id)
    newest = repository.list_sessions(ResearchStatus.RUNNING, limit=3, newest_first=True)
    assert [s.id for s in newest] == running[::-1][:3]
    assert all(s.status is ResearchStatus.RUNNING for s in newest)
    oldest = repository.list_sessions(ResearchStatus.RUNNING, limit=3)
    assert [s.id for s in oldest] == running[:3]
    assert repository.list_sessions(ResearchStatus.PENDING, limit=2, newest_first=True) == [
        repository.get_session(sessions[7].id),
        repository.get_session(sessions[5].id),
    ]


def test_list_sessions_ties_break_by_id_in_both_directions(database: Database) -> None:
    repository = ResearchRepository(database, clock=lambda: NOW)
    created = [repository.create_session(f"q{n}") for n in range(6)]
    by_id = sorted(created, key=lambda session: str(session.id))
    assert repository.list_sessions() == by_id
    assert repository.list_sessions(newest_first=True) == by_id[::-1]
    assert repository.list_sessions(limit=2, newest_first=True) == by_id[::-1][:2]
    assert repository.list_sessions(newest_first=True) == repository.list_sessions(
        newest_first=True
    )


@pytest.mark.parametrize("newest_first", [1, 0, None, "yes"])
def test_list_sessions_rejects_non_bool_newest_first(
    repository: ResearchRepository, newest_first
) -> None:
    with pytest.raises(ValueError):
        repository.list_sessions(newest_first=newest_first)


def test_list_sessions_newest_first_is_keyword_only(repository: ResearchRepository) -> None:
    with pytest.raises(TypeError):
        repository.list_sessions(None, 10, True)


@pytest.mark.parametrize("question", ["", "   ", "x" * 2001, "bad\x00text", None, 5])
def test_create_session_rejects_bad_question(repository: ResearchRepository, question) -> None:
    with pytest.raises(ValueError):
        repository.create_session(question)


def test_create_session_rejects_non_level(repository: ResearchRepository) -> None:
    with pytest.raises(ValueError):
        repository.create_session("q", "quick")


def test_timestamps_are_utc_iso_strings_and_uuids_text(database: Database) -> None:
    local = timezone(timedelta(hours=9))
    repository = ResearchRepository(
        database, clock=lambda: datetime(2026, 10, 7, 21, 0, tzinfo=local)
    )
    session = repository.create_session("q")
    with sqlite3.connect(database.path) as connection:
        row = connection.execute("SELECT id, created_at FROM research_sessions").fetchone()
    assert row == (str(session.id), "2026-10-07T12:00:00.000000Z")
    assert session.created_at == NOW
    with pytest.raises(ValueError):
        repository.add_source(
            session.id,
            url="https://example.test/",
            final_url="https://example.test/",
            retrieved_at=datetime(2026, 10, 7),
            content_digest=DIGEST,
        )


ALL_STATUSES = list(ResearchStatus)


@pytest.mark.parametrize("expected", ALL_STATUSES)
@pytest.mark.parametrize("new", ALL_STATUSES)
def test_every_transition_pair(
    repository: ResearchRepository, expected: ResearchStatus, new: ResearchStatus
) -> None:
    session = _status_session(repository, expected)
    reason = FailureReason.SEARCH_FAILED if new is ResearchStatus.FAILED else None
    allowed = new in ALLOWED_TRANSITIONS[expected]
    if new is ResearchStatus.COMPLETED:
        # Completion is only reachable through set_result.
        with pytest.raises(InvalidTransition):
            repository.transition(session.id, expected, new)
        if expected is ResearchStatus.RUNNING:
            done = repository.set_result(session.id, "answer")
            assert done.status is ResearchStatus.COMPLETED and done.result_text == "answer"
        else:
            with pytest.raises(ResearchStateChanged):
                repository.set_result(session.id, "answer")
        return
    if allowed:
        moved = repository.transition(session.id, expected, new, failure_reason=reason)
        assert moved.status is new
        assert moved.failure_reason == reason
        assert repository.get_session(session.id) == moved
    else:
        with pytest.raises(InvalidTransition):
            repository.transition(session.id, expected, new, failure_reason=reason)
        assert repository.get_session(session.id).status is expected


def test_terminal_states_are_immutable(repository: ResearchRepository) -> None:
    for terminal in (ResearchStatus.FAILED, ResearchStatus.COMPLETED, ResearchStatus.CANCELLED):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
        session = _status_session(repository, terminal)
        with pytest.raises(ResearchStateChanged):
            repository.add_query(session.id, "late")
        with pytest.raises(ResearchStateChanged):
            _source(repository, session.id)
        with pytest.raises(ResearchStateChanged):
            repository.set_result(session.id, "overwrite")
        assert repository.get_session(session.id) == session


def test_transition_validates_failure_reason(repository: ResearchRepository) -> None:
    session = _status_session(repository, ResearchStatus.RUNNING)
    with pytest.raises(ValueError):
        repository.transition(session.id, ResearchStatus.RUNNING, ResearchStatus.FAILED)
    with pytest.raises(ValueError):
        repository.transition(
            session.id, ResearchStatus.RUNNING, ResearchStatus.FAILED, failure_reason="boom: 500"
        )
    with pytest.raises(ValueError):
        repository.transition(
            session.id,
            ResearchStatus.RUNNING,
            ResearchStatus.WAITING,
            failure_reason=FailureReason.TIMEOUT,
        )
    assert repository.get_session(session.id).status is ResearchStatus.RUNNING


def test_failure_reason_is_a_fixed_code_enforced_by_storage(
    repository: ResearchRepository, database: Database
) -> None:
    session = _status_session(repository, ResearchStatus.FAILED)
    assert session.failure_reason is FailureReason.TIMEOUT
    with sqlite3.connect(database.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE research_sessions SET failure_reason = 'free text from upstream'"
        )


def test_transition_missing_session(repository: ResearchRepository) -> None:
    with pytest.raises(ResearchSessionNotFound):
        repository.transition(uuid4(), ResearchStatus.PENDING, ResearchStatus.RUNNING)


def test_stale_expected_status_is_refused(repository: ResearchRepository) -> None:
    session = repository.create_session("q")
    repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    with pytest.raises(ResearchStateChanged):
        repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.CANCELLED)
    assert repository.get_session(session.id).status is ResearchStatus.RUNNING


def test_compare_and_swap_race_has_one_winner(database: Database) -> None:
    first = ResearchRepository(database)
    second = ResearchRepository(database)
    session = first.create_session("q")
    first.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(repository: ResearchRepository, new: ResearchStatus) -> None:
        barrier.wait()
        try:
            repository.transition(session.id, ResearchStatus.RUNNING, new)
            outcomes.append(new.value)
        except ResearchStateChanged:
            outcomes.append("lost")

    threads = [
        threading.Thread(target=attempt, args=(first, ResearchStatus.WAITING)),
        threading.Thread(target=attempt, args=(second, ResearchStatus.CANCELLED)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes).count("lost") == 1
    winner = next(item for item in outcomes if item != "lost")
    assert first.get_session(session.id).status.value == winner


def test_set_result_only_when_running_and_bounded(repository: ResearchRepository) -> None:
    pending = repository.create_session("q")
    with pytest.raises(ResearchStateChanged):
        repository.set_result(pending.id, "too early")
    running = _status_session(repository, ResearchStatus.RUNNING)
    for bad in ("", "  ", "x" * 50_001, None):
        with pytest.raises(ValueError):
            repository.set_result(running.id, bad)
    assert repository.get_session(running.id).result_text is None
    done = repository.set_result(running.id, "x" * 50_000)
    assert done.status is ResearchStatus.COMPLETED
    assert done.failure_reason is None
    with pytest.raises(ResearchSessionNotFound):
        repository.set_result(uuid4(), "orphan")


def test_queries_keep_order(repository: ResearchRepository) -> None:
    session = repository.create_session("q")
    first = repository.add_query(session.id, "first search")
    second = repository.add_query(session.id, "second search")
    assert (first.position, second.position) == (0, 1)
    assert repository.list_queries(session.id) == [first, second]
    with pytest.raises(ValueError):
        repository.add_query(session.id, "x" * 501)
    with pytest.raises(ResearchSessionNotFound):
        repository.add_query(uuid4(), "orphan")


def test_add_source_roundtrip_and_dedupe(repository: ResearchRepository) -> None:
    session = repository.create_session("q")
    published = datetime(2026, 1, 2, tzinfo=UTC)
    source = _source(
        repository,
        session.id,
        publisher="Example Org",
        published_at=published,
        source_type=SourceType.DOCS,
    )
    assert source.session_id == session.id
    assert source.published_at == published
    assert source.source_type is SourceType.DOCS
    assert source.evaluation == SourceEvaluation()
    assert repository.get_source(source.id) == source
    duplicate = _source(repository, session.id, title="Different title on re-read")
    assert duplicate == source
    assert repository.list_sources(session.id) == [source]
    other_page = _source(repository, session.id, content_digest=hashlib.sha256(b"x").hexdigest())
    other_url = _source(repository, session.id, final_url="https://example.test/b")
    assert len({source.id, other_page.id, other_url.id}) == 3
    other_session = repository.create_session("other")
    assert _source(repository, other_session.id).id != source.id
    assert repository.get_source(uuid4()) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "file:///etc/passwd"},
        {"final_url": "javascript:alert(1)"},
        {"url": "/relative"},
        {"url": "https://exa mple.test/"},
        {"url": "https://example.test/" + "a" * 2048},
        {"content_digest": "abc"},
        {"content_digest": DIGEST.upper()},
        {"title": "x" * 501},
        {"title": "  "},
        {"source_type": "official"},
    ],
)
def test_add_source_rejects_invalid_values(repository: ResearchRepository, overrides) -> None:
    session = repository.create_session("q")
    with pytest.raises(ValueError):
        _source(repository, session.id, **overrides)
    assert repository.list_sources(session.id) == []


def test_add_source_unknown_session(repository: ResearchRepository) -> None:
    with pytest.raises(ResearchSessionNotFound):
        _source(repository, uuid4())


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf"), True, "0.5"])
def test_evaluation_rating_range_is_validated(bad) -> None:
    for name in ("authority", "freshness", "primary", "relevance", "agreement"):
        with pytest.raises(ValueError):
            SourceEvaluation(**{name: bad})


def test_evaluation_roundtrip_and_independent_ratings(repository: ResearchRepository) -> None:
    session = repository.create_session("q")
    source = _source(
        repository, session.id, evaluation=SourceEvaluation(authority=0.9, primary=0.0)
    )
    assert source.evaluation == SourceEvaluation(authority=0.9, primary=0.0)
    updated = repository.set_evaluation(
        source.id, SourceEvaluation(freshness=1.0, relevance=0.25, agreement=0.5)
    )
    assert updated.evaluation == SourceEvaluation(freshness=1.0, relevance=0.25, agreement=0.5)
    assert repository.get_source(source.id) == updated
    with pytest.raises(ValueError):
        repository.set_evaluation(source.id, {"authority": 0.5})
    with pytest.raises(ResearchIntegrityError):
        repository.set_evaluation(uuid4(), SourceEvaluation())


def test_storage_rejects_out_of_range_rating(
    repository: ResearchRepository, database: Database
) -> None:
    session = repository.create_session("q")
    _source(repository, session.id)
    with sqlite3.connect(database.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE research_sources SET relevance = 1.5")


def test_claim_links_to_source_in_same_session(repository: ResearchRepository) -> None:
    session = repository.create_session("q")
    source = _source(repository, session.id)
    claim = repository.add_claim(
        session.id,
        claim_text="X is true",
        source_id=source.id,
        quote="X is demonstrably true",
        quote_start=10,
        quote_end=32,
    )
    bare = repository.add_claim(session.id, claim_text="Y", source_id=source.id, quote="Y holds")
    assert (bare.quote_start, bare.quote_end) == (None, None)
    assert repository.list_claims(session.id) == [claim, bare]
    assert repository.list_claims_for_source(source.id) == [claim, bare]
    assert repository.list_claims(uuid4()) == []


def test_claim_for_foreign_session_source_is_rejected(
    repository: ResearchRepository, database: Database
) -> None:
    mine = repository.create_session("mine")
    theirs = repository.create_session("theirs")
    foreign = _source(repository, theirs.id)
    with pytest.raises(ResearchIntegrityError):
        repository.add_claim(mine.id, claim_text="c", source_id=foreign.id, quote="q")
    with pytest.raises(ResearchIntegrityError):
        repository.add_claim(mine.id, claim_text="c", source_id=uuid4(), quote="q")
    assert repository.list_claims(mine.id) == []
    # The database itself also refuses a cross-session link.
    with database.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO research_claims (id, session_id, claim_text, source_id, quote) "
            "VALUES ('c1', ?, 'c', ?, 'q')",
            (str(mine.id), str(foreign.id)),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quote": ""},
        {"quote": "q" * 501},
        {"claim_text": " "},
        {"quote_start": 1},
        {"quote_end": 5},
        {"quote_start": -1, "quote_end": 5},
        {"quote_start": 5, "quote_end": 5},
        {"quote_start": 6, "quote_end": 5},
        {"quote_start": 0, "quote_end": 10_000_001},
        {"quote_start": 0.5, "quote_end": 3},
        {"quote_start": True, "quote_end": 3},
    ],
)
def test_claim_validates_quote_and_offsets(repository: ResearchRepository, kwargs) -> None:
    session = repository.create_session("q")
    source = _source(repository, session.id)
    values = {"claim_text": "c", "source_id": source.id, "quote": "quote"}
    values.update(kwargs)
    with pytest.raises(ValueError):
        repository.add_claim(session.id, **values)
    assert repository.list_claims(session.id) == []


def test_claim_quote_of_exactly_500_characters_is_accepted(
    repository: ResearchRepository,
) -> None:
    session = repository.create_session("q")
    source = _source(repository, session.id)
    claim = repository.add_claim(session.id, claim_text="c", source_id=source.id, quote="q" * 500)
    assert len(claim.quote) == 500


def test_everything_is_durable_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first_db = Database(path)
    first_db.initialize()
    repository = ResearchRepository(first_db)
    session = repository.create_session("durable?", ResearchLevel.DEEP)
    repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    query = repository.add_query(session.id, "durable search")
    source = _source(repository, session.id, evaluation=SourceEvaluation(authority=0.7))
    claim = repository.add_claim(session.id, claim_text="c", source_id=source.id, quote="q")
    finished = repository.set_result(session.id, "answer")

    reopened_db = Database(path)
    reopened_db.initialize()
    reopened = ResearchRepository(reopened_db)
    assert reopened.get_session(session.id) == finished
    assert reopened.list_queries(session.id) == [query]
    assert reopened.list_sources(session.id) == [source]
    assert reopened.list_claims(session.id) == [claim]


def test_foreign_keys_are_enforced_and_there_is_no_delete_api(
    repository: ResearchRepository, database: Database
) -> None:
    assert not any(name.startswith(("delete", "remove")) for name in dir(repository))
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO research_queries (id, session_id, text, position, created_at) "
                "VALUES ('q1', 'no-such-session', 't', 0, 't')"
            )


def test_storage_failure_is_reported_without_details(tmp_path: Path) -> None:
    repository = ResearchRepository(Database(tmp_path / "missing" / "nothing.sqlite3"))
    with pytest.raises(ResearchRepositoryError, match="Research storage unavailable"):
        repository.create_session("q")
