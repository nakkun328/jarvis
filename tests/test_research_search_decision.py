"""Additional-search decision (JAR-48): fixed rules, fixed codes, honest caveats."""

import itertools
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.core.database import Database
from backend.research.levels import LEVEL_BUDGETS, LevelBudget
from backend.research.models import ConflictKind, ResearchLevel, ResearchStatus, SourceEvaluation
from backend.research.repository import ResearchRepository
from backend.research.search_decision import (
    Action,
    DecisionReason,
    DecisionThresholds,
    Gap,
    SearchState,
    decide_additional_search,
    find_gaps,
    search_state_from_repository,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
GOOD = SourceEvaluation(authority=0.9, freshness=0.9, relevance=0.8)


def state(**overrides) -> SearchState:
    values = {
        "level": ResearchLevel.STANDARD,
        "planned_queries": 3,
        "executed_queries": 3,
        "pages_read": 3,
        "evaluations": (GOOD, GOOD, GOOD),
    }
    values.update(overrides)
    return SearchState(**values)


def test_enough_good_sources_stop() -> None:
    decision = decide_additional_search(state())
    assert decision.action is Action.STOP
    assert decision.reason is DecisionReason.SUFFICIENT_COVERAGE
    assert decision.caveats == ()


def test_planned_queries_remaining_continue_even_after_the_extra_rounds() -> None:
    decision = decide_additional_search(state(executed_queries=2, search_rounds_done=9))
    assert (decision.action, decision.reason) == (
        Action.CONTINUE,
        DecisionReason.PLANNED_QUERIES_REMAINING,
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"elapsed_seconds": 300.0}, DecisionReason.TIME_BUDGET_EXHAUSTED),
        ({"pages_read": 8}, DecisionReason.PAGE_BUDGET_EXHAUSTED),
        ({"executed_queries": 5, "planned_queries": 5}, DecisionReason.QUERY_BUDGET_EXHAUSTED),
        ({"search_rounds_done": 2}, DecisionReason.SEARCH_ROUNDS_EXHAUSTED),
        (
            {"search_rounds_done": 1, "new_sources_last_round": 0, "evaluations": ()},
            DecisionReason.NO_NEW_SOURCES,
        ),
    ],
)
def test_budget_stops_are_not_overridden_by_gaps(overrides: dict, reason: DecisionReason) -> None:
    decision = decide_additional_search(state(unresolved_conflicts=2, **overrides))
    assert decision.action is Action.STOP and decision.reason is reason
    assert Gap.UNRESOLVED_CONFLICTS in decision.caveats  # never presented as complete


def test_memory_level_never_searches() -> None:
    decision = decide_additional_search(
        SearchState(level=ResearchLevel.MEMORY, unresolved_conflicts=1)
    )
    assert decision.action is Action.STOP
    assert decision.reason is DecisionReason.LEVEL_DOES_NOT_SEARCH


def test_gaps_continue_in_priority_order() -> None:
    def reason(**overrides):
        return decide_additional_search(state(**overrides)).reason

    assert reason(evaluations=()) is DecisionReason.NO_SOURCES
    assert reason(evaluations=(GOOD,)) is DecisionReason.FEW_RELEVANT_SOURCES
    assert reason(unresolved_conflicts=1) is DecisionReason.UNRESOLVED_CONFLICTS
    low_authority = SourceEvaluation(authority=0.3, freshness=0.9, relevance=0.8)
    assert reason(evaluations=(low_authority,) * 3) is DecisionReason.NO_AUTHORITATIVE_SOURCE
    stale = SourceEvaluation(authority=0.9, freshness=0.05, relevance=0.8)
    assert reason(evaluations=(stale,) * 3) is DecisionReason.STALE_SOURCES
    # priority: missing sources beat conflicts, conflicts beat authority
    assert reason(evaluations=(), unresolved_conflicts=3) is DecisionReason.NO_SOURCES
    assert (
        reason(evaluations=(low_authority,) * 3, unresolved_conflicts=1)
        is DecisionReason.UNRESOLVED_CONFLICTS
    )


def test_unknown_dates_or_ratings_are_not_treated_as_stale_or_good() -> None:
    undated = SourceEvaluation(authority=0.9, relevance=0.8)
    assert find_gaps(state(evaluations=(undated,) * 3)) == ()
    mixed = (SourceEvaluation(authority=0.9, freshness=0.05, relevance=0.8), undated, undated)
    assert Gap.STALE_SOURCES not in find_gaps(state(evaluations=mixed))
    unrated = SourceEvaluation()
    assert find_gaps(state(evaluations=(unrated,) * 3)) == (Gap.FEW_RELEVANT_SOURCES,)
    assert Gap.NO_AUTHORITATIVE_SOURCE not in find_gaps(state(evaluations=(unrated,) * 3))


def test_irrelevant_sources_do_not_count() -> None:
    off_topic = SourceEvaluation(authority=0.9, freshness=0.9, relevance=0.1)
    assert find_gaps(state(evaluations=(off_topic,) * 5)) == (Gap.FEW_RELEVANT_SOURCES,)


def test_thresholds_are_overridable_and_validated() -> None:
    strict = DecisionThresholds(min_relevant_sources=5)
    assert decide_additional_search(state(), thresholds=strict).reason is (
        DecisionReason.FEW_RELEVANT_SOURCES
    )
    relaxed = DecisionThresholds(min_relevant_sources=1, min_relevance=0.0)
    assert decide_additional_search(state(evaluations=(GOOD,)), thresholds=relaxed).action is (
        Action.STOP
    )
    for bad in (-0.1, 1.1, float("nan"), True, "0.5"):
        with pytest.raises(ValueError):
            DecisionThresholds(min_relevance=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DecisionThresholds(min_relevant_sources=-1)


def test_budget_can_be_supplied() -> None:
    tight = LevelBudget(ResearchLevel.STANDARD, 5, 6, 8, 1, 300.0)
    decision = decide_additional_search(state(evaluations=(), search_rounds_done=1), budget=tight)
    assert decision.reason is DecisionReason.SEARCH_ROUNDS_EXHAUSTED
    assert decision.caveats == (Gap.NO_SOURCES,)


def test_level_defaults_follow_the_budget_table() -> None:
    for level, budget in LEVEL_BUDGETS.items():
        if level is ResearchLevel.MEMORY:
            continue
        rounds = SearchState(
            level=level,
            planned_queries=1,
            executed_queries=1,
            search_rounds_done=budget.max_search_rounds,
        )
        assert decide_additional_search(rounds).reason is DecisionReason.SEARCH_ROUNDS_EXHAUSTED
        under = replace(rounds, search_rounds_done=budget.max_search_rounds - 1)
        assert decide_additional_search(under).action is Action.CONTINUE


def test_decision_is_pure_and_total_over_a_grid() -> None:
    values = itertools.product(
        list(ResearchLevel),
        (0, 1, 3, 40),
        (0, 1, 3),
        (0, 1, 5),
        (0.0, 100.0, 5000.0),
        (0, 2),
        (None, 0, 3),
    )
    for level, planned, executed, rounds, elapsed, conflicts, new in values:
        built = SearchState(
            level=level,
            planned_queries=planned,
            executed_queries=executed,
            search_rounds_done=rounds,
            pages_read=executed,
            elapsed_seconds=elapsed,
            evaluations=(GOOD,),
            unresolved_conflicts=conflicts,
            new_sources_last_round=new,
        )
        first = decide_additional_search(built)
        assert first == decide_additional_search(built)
        assert isinstance(first.action, Action) and isinstance(first.reason, DecisionReason)
        if first.action is Action.CONTINUE:
            budget = LEVEL_BUDGETS[level]
            assert built.elapsed_seconds < budget.total_timeout_seconds
            assert built.executed_queries < budget.max_queries
            assert built.pages_read < budget.max_pages


def test_state_validation() -> None:
    for bad in (
        {"planned_queries": -1},
        {"executed_queries": True},
        {"pages_read": 1.5},
        {"elapsed_seconds": float("inf")},
        {"elapsed_seconds": -1},
        {"unresolved_conflicts": "1"},
        {"new_sources_last_round": -2},
        {"evaluations": ("x",)},
        {"level": "huge"},
    ):
        with pytest.raises(ValueError):
            SearchState(**{"level": ResearchLevel.QUICK, **bad})
    with pytest.raises(ValueError):
        decide_additional_search("not a state")  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        state().pages_read = 9  # type: ignore[misc]


def test_every_reason_code_is_a_fixed_lowercase_identifier() -> None:
    for member in (*DecisionReason, *Gap, *Action):
        assert member.value == member.value.lower()
        assert member.value.replace("_", "").isalpha()


# ----- from the repository -----


@pytest.fixture
def repository(tmp_path: Path) -> ResearchRepository:
    database = Database(tmp_path / "decision.sqlite3")
    database.initialize()
    return ResearchRepository(database, clock=lambda: NOW)


def test_state_from_repository(repository: ResearchRepository) -> None:
    session = repository.create_session("q", ResearchLevel.DEEP)
    repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repository.add_query(session.id, "one")
    repository.add_query(session.id, "two")
    sources = [
        repository.add_source(
            session.id,
            url=f"https://example.test/{n}",
            final_url=f"https://example.test/{n}",
            retrieved_at=NOW,
            content_digest=f"{n:064x}",
            evaluation=GOOD,
        )
        for n in (1, 2)
    ]
    claims = [
        repository.add_claim(session.id, claim_text=f"claim {n}", source_id=s.id, quote="quote")
        for n, s in enumerate(sources)
    ]
    repository.add_conflict(
        session.id, ConflictKind.NUMBER_MISMATCH, claims[0].id, other_claim_id=claims[1].id
    )
    built = search_state_from_repository(
        repository,
        session.id,
        executed_queries=1,
        search_rounds_done=0,
        pages_read=2,
        elapsed_seconds=10.0,
    )
    assert built.level is ResearchLevel.DEEP
    assert built.planned_queries == 2 and built.executed_queries == 1
    assert built.evaluations == (GOOD, GOOD)
    assert built.unresolved_conflicts == 1
    assert decide_additional_search(built).reason is DecisionReason.PLANNED_QUERIES_REMAINING
    with pytest.raises(ValueError):
        search_state_from_repository(
            repository,
            __import__("uuid").uuid4(),
            executed_queries=0,
            search_rounds_done=0,
            pages_read=0,
            elapsed_seconds=0,
        )
