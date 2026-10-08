"""Persisted source classification and rating reasons (JAR-42/43/45), v7 to v8 migration, DTO."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.research import create_research_router
from backend.core.database import Database
from backend.research.conflicts import detect_and_store_conflicts
from backend.research.evaluation import evaluate_and_store
from backend.research.models import (
    RATING_REASONS,
    Basis,
    ConflictResolution,
    RatingName,
    RatingReason,
    ResearchStatus,
    SourceEvaluation,
    SourceType,
)
from backend.research.repository import (
    ResearchIntegrityError,
    ResearchRepository,
    ResearchStateChanged,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "persist.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def repository(database: Database) -> ResearchRepository:
    return ResearchRepository(database, clock=lambda: NOW)


def _running(repository: ResearchRepository, question: str = "latest Python version"):
    session = repository.create_session(question)
    repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    return session


def _source(repository: ResearchRepository, session_id, name: str = "a", **overrides):
    values = {
        "url": f"https://docs.example.test/{name}",
        "final_url": f"https://docs.example.test/{name}",
        "retrieved_at": NOW,
        "content_digest": hashlib.sha256(name.encode()).hexdigest(),
        "title": "Python documentation",
        "published_at": NOW - timedelta(days=180),
    }
    values.update(overrides)
    return repository.add_source(session_id, **values)


# ----- classification -----


def test_new_source_has_no_recorded_classification(repository: ResearchRepository) -> None:
    source = _source(repository, _running(repository).id)
    assert source.classification_rule is None and source.classification_basis is None
    assert all(source.reasons.get(name) == () for name in RatingName)


def test_set_source_classification_is_stored_and_idempotent(
    repository: ResearchRepository,
) -> None:
    source = _source(repository, _running(repository).id)
    stored = repository.set_source_classification(
        source.id, SourceType.DOCS, rule_id="docs_host", basis=Basis.HOST
    )
    assert (stored.source_type, stored.classification_rule, stored.classification_basis) == (
        SourceType.DOCS,
        "docs_host",
        Basis.HOST,
    )
    again = repository.set_source_classification(
        source.id, SourceType.DOCS, rule_id="docs_host", basis=Basis.HOST
    )
    assert again == stored == repository.get_source(source.id)
    assert repository.list_sources(source.session_id) == [stored]


@pytest.mark.parametrize("rule_id", ["", "Docs", "docs host", "docs;DROP", "9x", "x" * 65, "ü"])
def test_rule_id_must_be_a_short_identifier(repository: ResearchRepository, rule_id: str) -> None:
    source = _source(repository, _running(repository).id)
    with pytest.raises(ValueError):
        repository.set_source_classification(
            source.id, SourceType.DOCS, rule_id=rule_id, basis=Basis.HOST
        )
    assert repository.get_source(source.id).classification_rule is None


def test_classification_argument_types_are_checked(repository: ResearchRepository) -> None:
    source = _source(repository, _running(repository).id)
    with pytest.raises(ValueError):
        repository.set_source_classification(source.id, "docs", rule_id="x", basis=Basis.HOST)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        repository.set_source_classification(source.id, SourceType.DOCS, rule_id="x", basis="host")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        repository.set_source_classification("nope", SourceType.DOCS, rule_id="x", basis=Basis.HOST)  # type: ignore[arg-type]
    with pytest.raises(ResearchIntegrityError):
        repository.set_source_classification(
            uuid4(), SourceType.DOCS, rule_id="x", basis=Basis.HOST
        )


def test_classification_is_refused_after_the_session_is_final(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    source = _source(repository, session.id)
    repository.transition(session.id, ResearchStatus.RUNNING, ResearchStatus.CANCELLED)
    with pytest.raises(ResearchStateChanged):
        repository.set_source_classification(
            source.id, SourceType.DOCS, rule_id="docs_host", basis=Basis.HOST
        )
    assert repository.get_source(source.id).classification_rule is None


# ----- reasons -----


def test_reasons_are_stored_per_rating_and_replace_only_named_ratings(
    repository: ResearchRepository,
) -> None:
    source = _source(repository, _running(repository).id)
    stored = repository.set_evaluation(
        source.id,
        SourceEvaluation(authority=0.85, relevance=0.5),
        reasons={
            RatingName.AUTHORITY: [RatingReason.AUTHORITY_BY_TYPE],
            RatingName.AGREEMENT: [
                RatingReason.AGREEMENT_MIXED,
                RatingReason.AGREEMENT_DATE_MISMATCH,
            ],
        },
    )
    assert stored.reasons.authority == (RatingReason.AUTHORITY_BY_TYPE,)
    assert stored.reasons.agreement == (
        RatingReason.AGREEMENT_MIXED,
        RatingReason.AGREEMENT_DATE_MISMATCH,
    )
    assert stored.reasons.freshness == ()
    again = repository.set_evaluation(
        source.id, stored.evaluation, reasons={RatingName.AUTHORITY: []}
    )
    assert again.reasons.authority == ()
    assert again.reasons.agreement == stored.reasons.agreement
    # without `reasons` the stored codes are left alone
    assert repository.set_evaluation(source.id, again.evaluation).reasons == again.reasons
    assert repository.get_source(source.id).reasons == again.reasons


def test_reasons_order_is_kept(repository: ResearchRepository) -> None:
    source = _source(repository, _running(repository).id)
    codes = [
        RatingReason.AGREEMENT_VERBATIM_SUPPORT,
        RatingReason.AGREEMENT_CORROBORATED,
        RatingReason.AGREEMENT_NUMBER_MISMATCH,
    ]
    stored = repository.set_evaluation(
        source.id, SourceEvaluation(), reasons={RatingName.AGREEMENT: codes}
    )
    assert list(stored.reasons.agreement) == codes


@pytest.mark.parametrize(
    "reasons",
    [
        {RatingName.AUTHORITY: [RatingReason.FRESHNESS_DECAY]},  # belongs to another rating
        {RatingName.AUTHORITY: ["authority_by_type"]},  # not an enum member
        {RatingName.AUTHORITY: [RatingReason.AUTHORITY_BY_TYPE] * 2},  # repeated
        {RatingName.AGREEMENT: list(RATING_REASONS[RatingName.AGREEMENT])[:5]},  # too many
        {"authority": [RatingReason.AUTHORITY_BY_TYPE]},  # key not a RatingName
        {RatingName.AUTHORITY: "authority_by_type"},  # a bare string
        [RatingReason.AUTHORITY_BY_TYPE],  # not a mapping
    ],
)
def test_invalid_reasons_are_refused_and_change_nothing(
    repository: ResearchRepository, reasons: object
) -> None:
    source = _source(repository, _running(repository).id)
    before = repository.get_source(source.id)
    with pytest.raises(ValueError):
        repository.set_evaluation(source.id, SourceEvaluation(authority=0.1), reasons=reasons)  # type: ignore[arg-type]
    assert repository.get_source(source.id) == before


def test_database_only_accepts_identifier_shaped_reasons(
    repository: ResearchRepository,
) -> None:
    source = _source(repository, _running(repository).id)
    with repository.database.connect() as connection:
        for bad in ("Free text", "has space", "", "x" * 65, "<script>"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO research_source_reasons VALUES (?, 'authority', 0, ?)",
                    (str(source.id), bad),
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO research_source_reasons VALUES (?, 'primary', 0, 'x')",
                (str(source.id),),
            )


def test_set_source_assessment_is_one_atomic_write(repository: ResearchRepository) -> None:
    source = _source(repository, _running(repository).id)
    with pytest.raises(ValueError):
        repository.set_source_assessment(
            source.id,
            source_type=SourceType.DOCS,
            rule_id="docs_host",
            basis=Basis.HOST,
            evaluation=SourceEvaluation(authority=0.85),
            reasons={RatingName.AUTHORITY: [RatingReason.FRESHNESS_DECAY]},
        )
    stored = repository.get_source(source.id)
    assert stored.source_type is SourceType.UNKNOWN and stored.evaluation.authority is None


# ----- evaluate_and_store -----


def test_evaluate_and_store_persists_type_rule_basis_and_reasons(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    source = _source(repository, session.id)
    stored = evaluate_and_store(
        repository, source, "latest Python version", text="The latest Python version is out."
    )
    assert stored.source_type is SourceType.DOCS
    assert (stored.classification_rule, stored.classification_basis) == ("docs_host", Basis.HOST)
    assert stored.reasons.authority == (RatingReason.AUTHORITY_BY_TYPE,)
    assert stored.reasons.freshness == (RatingReason.FRESHNESS_DECAY,)
    assert stored.reasons.relevance == (RatingReason.RELEVANCE_OVERLAP,)
    assert stored.reasons.agreement == ()
    assert repository.get_source(source.id) == stored


def test_evaluate_and_store_keeps_agreement_and_stored_classification(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    source = _source(
        repository,
        session.id,
        url="https://example.test/docs/x",
        final_url="https://example.test/docs/x",
    )
    repository.set_evaluation(
        source.id,
        SourceEvaluation(agreement=0.5),
        reasons={RatingName.AGREEMENT: [RatingReason.AGREEMENT_MIXED]},
    )
    first = evaluate_and_store(repository, repository.get_source(source.id), "Python docs")
    assert first.classification_rule == "docs_path" and first.classification_basis is Basis.PATH
    assert first.evaluation.authority == 0.6  # path-based type is capped
    assert first.reasons.authority == (RatingReason.AUTHORITY_CAPPED_WEAK_BASIS,)
    assert first.evaluation.agreement == 0.5
    assert first.reasons.agreement == (RatingReason.AGREEMENT_MIXED,)
    # re-running from the stored row keeps the recorded rule and the cap
    second = evaluate_and_store(repository, first, "Python docs")
    assert second == first


def test_a_caller_supplied_type_is_recorded_as_provided(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    source = _source(repository, session.id, source_type=SourceType.NEWS)
    stored = evaluate_and_store(repository, source, "q")
    assert stored.source_type is SourceType.NEWS
    assert (stored.classification_rule, stored.classification_basis) == (
        "provided",
        Basis.PROVIDED,
    )
    assert stored.evaluation.authority == 0.6


def test_evaluate_and_store_on_an_unclassifiable_source(repository: ResearchRepository) -> None:
    session = _running(repository)
    source = _source(
        repository,
        session.id,
        url="https://example.test/x",
        final_url="https://example.test/x",
        title=None,
        published_at=None,
    )
    stored = evaluate_and_store(repository, source, "something")
    assert stored.source_type is SourceType.UNKNOWN
    assert (stored.classification_rule, stored.classification_basis) == ("no_rule", Basis.DEFAULT)
    assert stored.reasons.authority == (RatingReason.AUTHORITY_UNCLASSIFIED,)
    assert stored.reasons.freshness == (RatingReason.FRESHNESS_UNKNOWN_DATE,)
    assert stored.reasons.relevance == (RatingReason.RELEVANCE_NO_TEXT,)


def test_evaluate_and_store_failure_leaves_the_source_unchanged(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    source = _source(repository, session.id)
    repository.transition(session.id, ResearchStatus.RUNNING, ResearchStatus.CANCELLED)
    with pytest.raises(ResearchStateChanged):
        evaluate_and_store(repository, source, "q")
    assert repository.get_source(source.id) == source


# ----- migration v7 -> v8 -----


def _downgrade_to_v7(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE research_conflicts")
        connection.execute("DROP TABLE research_source_reasons")
        connection.execute("DROP INDEX research_claims_by_session_id")
        connection.execute("ALTER TABLE research_sources DROP COLUMN classification_rule")
        connection.execute("ALTER TABLE research_sources DROP COLUMN classification_basis")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute("PRAGMA user_version = 7")


def test_v7_database_with_research_data_upgrades_and_stays_valid(tmp_path: Path) -> None:
    path = tmp_path / "v7.sqlite3"
    database = Database(path)
    database.initialize()
    repository = ResearchRepository(database, clock=lambda: NOW)
    session = _running(repository)
    source = _source(
        repository,
        session.id,
        source_type=SourceType.DOCS,
        evaluation=SourceEvaluation(authority=0.85, freshness=0.5),
    )
    repository.add_claim(session.id, claim_text="A claim", source_id=source.id, quote="a quote")
    _downgrade_to_v7(path)
    assert not database.is_ready()

    database.initialize()

    assert database.is_ready()
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
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT source_type, classification_rule, classification_basis, authority "
            "FROM research_sources"
        ).fetchall() == [("docs", None, None, 0.85)]
    old = repository.get_source(source.id)
    assert old.source_type is SourceType.DOCS and old.classification_rule is None
    assert old.reasons.authority == () and old.evaluation.freshness == 0.5
    assert len(repository.list_claims(session.id)) == 1
    assert repository.list_conflicts(session.id) == []
    # old rows accept the new data
    updated = evaluate_and_store(repository, old, "latest Python version")
    assert updated.classification_rule == "provided"
    # a second initialize is a no-op that keeps everything
    database.initialize()
    assert repository.get_source(source.id) == updated


def test_failed_v8_migration_rolls_back_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "failed-v8.sqlite3"
    database = Database(path)
    database.initialize()
    _downgrade_to_v7(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_v8 BEFORE INSERT ON schema_migrations "
            "WHEN NEW.version = 8 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
    from backend.core.database import DatabaseError

    with pytest.raises(DatabaseError, match="Could not initialize"):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        columns = [r[1] for r in connection.execute("PRAGMA table_info(research_sources)")]
    assert "research_conflicts" not in names and "research_source_reasons" not in names
    assert "classification_rule" not in columns


# ----- API DTO -----


def _client(repository: ResearchRepository) -> TestClient:
    app = FastAPI()
    app.include_router(create_research_router(repository))
    return TestClient(app)


def test_dto_exposes_classification_reasons_and_conflicts_as_codes_only(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    first = _source(repository, session.id, "a")
    second = _source(repository, session.id, "b")
    for source in (first, second):
        evaluate_and_store(repository, source, "package size")
    claims = [
        repository.add_claim(session.id, claim_text=text, source_id=s.id, quote=text[:-1])
        for text, s in (("The package is 25 MB.", first), ("The package is 40 MB.", second))
    ]
    [conflict] = detect_and_store_conflicts(repository, session.id)
    repository.set_evaluation(
        first.id,
        repository.get_source(first.id).evaluation,
        reasons={RatingName.AGREEMENT: [RatingReason.AGREEMENT_NUMBER_MISMATCH]},
    )
    body = _client(repository).get(f"/api/research/sessions/{session.id}").json()
    source_dto = next(s for s in body["sources"] if s["id"] == str(first.id))
    assert source_dto["classification"] == {"rule": "docs_host", "basis": "host"}
    assert source_dto["reasons"] == {
        "authority": ["authority_by_type"],
        "freshness": ["freshness_decay"],
        "relevance": ["relevance_title_only"],
        "agreement": ["agreement_number_mismatch"],
    }
    assert set(source_dto["reasons"]) == {r.value for r in RatingName}
    [conflict_dto] = body["conflicts"]
    assert conflict_dto == {
        "id": str(conflict.id),
        "kind": "number_mismatch",
        "status": "open",
        "resolution": None,
        "claim_a_id": str(conflict.claim_a_id),
        "source_a_id": str(conflict.source_a_id),
        "claim_b_id": str(conflict.claim_b_id),
        "source_b_id": str(conflict.source_b_id),
        "detected_at": "2026-10-07T12:00:00.000000Z",
        "resolved_at": None,
    }
    assert {conflict_dto["claim_a_id"], conflict_dto["claim_b_id"]} == {str(c.id) for c in claims}
    # no claim or page text leaks into the conflict
    assert "MB" not in str(conflict_dto)


def test_dto_for_a_legacy_source_and_a_resolved_conflict(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    source = _source(repository, session.id)
    body = _client(repository).get(f"/api/research/sessions/{session.id}").json()
    [dto] = body["sources"]
    assert dto["classification"] == {"rule": None, "basis": None}
    assert dto["reasons"] == {name.value: [] for name in RatingName}
    assert body["conflicts"] == []
    assert source.id  # silence unused


def test_dto_shows_a_resolved_conflict_with_its_fixed_code(
    repository: ResearchRepository,
) -> None:
    session = _running(repository)
    first, second = _source(repository, session.id, "a"), _source(repository, session.id, "b")
    claims = [
        repository.add_claim(
            session.id, claim_text="The package is 25 MB.", source_id=first.id, quote="quote text"
        ),
        repository.add_claim(
            session.id, claim_text="The package is 40 MB.", source_id=second.id, quote="quote text"
        ),
    ]
    [conflict] = detect_and_store_conflicts(repository, session.id)
    repository.resolve_conflict(conflict.id, ConflictResolution.BOTH_REPORTED)
    [dto] = _client(repository).get(f"/api/research/sessions/{session.id}").json()["conflicts"]
    assert dto["status"] == "resolved" and dto["resolution"] == "both_reported"
    assert dto["resolved_at"] == "2026-10-07T12:00:00.000000Z"
    assert claims
