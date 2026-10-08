"""Additional-search decision (JAR-48): continue or stop, by fixed rules and fixed codes.

``decide_additional_search`` looks at a snapshot of the session (``SearchState``) and the
level's budget (``levels.LEVEL_BUDGETS``) and returns ``continue`` or ``stop`` with one reason
code. It is pure: no network, no model, no clock. The caller measures elapsed time and counts
what it did.

Order of the rules, first match decides:

1. Budget stops, which no quality gap overrides: the level does not search
   (``level_does_not_search``), time is used up (``time_budget_exhausted``), the page budget
   is used up (``page_budget_exhausted``), the query budget is used up
   (``query_budget_exhausted``).
2. Planned queries that were not executed yet: ``continue`` (``planned_queries_remaining``).
   That finishes the first pass; it does not use up an extra round.
3. The extra rounds of the level are used up (``search_rounds_exhausted``), or the last round
   found no new source (``no_new_sources``): stop.
4. Gaps in the evidence, first one found decides ``continue``: no sources at all
   (``no_sources``), too few relevant sources (``few_relevant_sources``), open conflicts
   (``unresolved_conflicts``), no authoritative source (``no_authoritative_source``), only
   stale sources (``stale_sources``).
5. No gap: stop (``sufficient_coverage``).

Whenever the decision is ``stop`` while gaps remain, the gaps are listed as ``caveats`` so a
stop caused by budget is never presented as completeness. Unresolved conflicts are always a
caveat when they exist.

The thresholds are proposals, not measured values, and every one can be overridden with
``DecisionThresholds``. "Enough sources" is a count and a set of ratings, which are themselves
heuristics; the decision says nothing about whether the answer is correct.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from backend.research.levels import LevelBudget, budget_for
from backend.research.models import ConflictStatus, ResearchLevel, SourceEvaluation
from backend.research.repository import ResearchRepository


class Action(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


class DecisionReason(StrEnum):
    """Fixed codes: why the decision is what it is."""

    LEVEL_DOES_NOT_SEARCH = "level_does_not_search"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    PAGE_BUDGET_EXHAUSTED = "page_budget_exhausted"
    QUERY_BUDGET_EXHAUSTED = "query_budget_exhausted"
    SEARCH_ROUNDS_EXHAUSTED = "search_rounds_exhausted"
    NO_NEW_SOURCES = "no_new_sources"
    SUFFICIENT_COVERAGE = "sufficient_coverage"
    PLANNED_QUERIES_REMAINING = "planned_queries_remaining"
    NO_SOURCES = "no_sources"
    FEW_RELEVANT_SOURCES = "few_relevant_sources"
    UNRESOLVED_CONFLICTS = "unresolved_conflicts"
    NO_AUTHORITATIVE_SOURCE = "no_authoritative_source"
    STALE_SOURCES = "stale_sources"


class Gap(StrEnum):
    """A shortfall in the evidence, in priority order. Listed as caveats on a stop."""

    NO_SOURCES = "no_sources"
    FEW_RELEVANT_SOURCES = "few_relevant_sources"
    UNRESOLVED_CONFLICTS = "unresolved_conflicts"
    NO_AUTHORITATIVE_SOURCE = "no_authoritative_source"
    STALE_SOURCES = "stale_sources"


_GAP_REASON = {
    Gap.NO_SOURCES: DecisionReason.NO_SOURCES,
    Gap.FEW_RELEVANT_SOURCES: DecisionReason.FEW_RELEVANT_SOURCES,
    Gap.UNRESOLVED_CONFLICTS: DecisionReason.UNRESOLVED_CONFLICTS,
    Gap.NO_AUTHORITATIVE_SOURCE: DecisionReason.NO_AUTHORITATIVE_SOURCE,
    Gap.STALE_SOURCES: DecisionReason.STALE_SOURCES,
}

#: Relevant sources wanted before the evidence counts as broad enough, by level.
MIN_RELEVANT_SOURCES: Mapping[ResearchLevel, int] = MappingProxyType(
    {
        ResearchLevel.MEMORY: 0,
        ResearchLevel.QUICK: 2,
        ResearchLevel.STANDARD: 3,
        ResearchLevel.DEEP: 4,
        ResearchLevel.EXTENSIVE: 5,
    }
)


@dataclass(frozen=True)
class DecisionThresholds:
    """Ratings that make a source count. ``min_relevant_sources=None`` uses the level's row."""

    min_relevance: float = 0.4
    min_authority: float = 0.6
    min_freshness: float = 0.3
    min_relevant_sources: int | None = None

    def __post_init__(self) -> None:
        for value in (self.min_relevance, self.min_authority, self.min_freshness):
            _check_unit(value, "thresholds")
        if self.min_relevant_sources is not None:
            _check_count(self.min_relevant_sources, "min_relevant_sources")


@dataclass(frozen=True)
class SearchState:
    """What the caller knows about the session so far.

    ``planned_queries`` counts all planned queries, ``executed_queries`` those already
    searched. ``search_rounds_done`` counts extra rounds after the first pass.
    ``new_sources_last_round`` is the number of new sources the last extra round added
    (``None`` before any extra round).
    """

    level: ResearchLevel
    planned_queries: int = 0
    executed_queries: int = 0
    search_rounds_done: int = 0
    pages_read: int = 0
    elapsed_seconds: float = 0.0
    evaluations: tuple[SourceEvaluation, ...] = ()
    unresolved_conflicts: int = 0
    new_sources_last_round: int | None = None

    def __post_init__(self) -> None:
        ResearchLevel(self.level)
        for name in (
            "planned_queries",
            "executed_queries",
            "search_rounds_done",
            "pages_read",
            "unresolved_conflicts",
        ):
            _check_count(getattr(self, name), name)
        if self.new_sources_last_round is not None:
            _check_count(self.new_sources_last_round, "new_sources_last_round")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, int | float)
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be a non-negative number")
        if not all(isinstance(item, SourceEvaluation) for item in self.evaluations):
            raise ValueError("evaluations must be SourceEvaluation items")


@dataclass(frozen=True)
class SearchDecision:
    action: Action
    reason: DecisionReason
    caveats: tuple[Gap, ...] = ()


def _check_count(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _check_unit(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{name} must be numbers from 0 to 1")


def find_gaps(state: SearchState, thresholds: DecisionThresholds | None = None) -> tuple[Gap, ...]:
    """The shortfalls in the evidence of ``state``, in priority order."""
    limits = thresholds or DecisionThresholds()
    wanted = (
        limits.min_relevant_sources
        if limits.min_relevant_sources is not None
        else MIN_RELEVANT_SOURCES[ResearchLevel(state.level)]
    )
    gaps: list[Gap] = []
    relevant = [
        e
        for e in state.evaluations
        if e.relevance is not None and e.relevance >= limits.min_relevance
    ]
    if not state.evaluations:
        gaps.append(Gap.NO_SOURCES)
    elif len(relevant) < wanted:
        gaps.append(Gap.FEW_RELEVANT_SOURCES)
    if state.unresolved_conflicts > 0:
        gaps.append(Gap.UNRESOLVED_CONFLICTS)
    if relevant:
        if not any(
            e.authority is not None and e.authority >= limits.min_authority for e in relevant
        ):
            gaps.append(Gap.NO_AUTHORITATIVE_SOURCE)
        dated = [e for e in relevant if e.freshness is not None]
        # Stale only when every relevant source has a known date and none of them is fresh.
        if (
            len(dated) == len(relevant)
            and dated
            and all(e.freshness < limits.min_freshness for e in dated)  # type: ignore[operator]
        ):
            gaps.append(Gap.STALE_SOURCES)
    return tuple(gaps)


def decide_additional_search(
    state: SearchState,
    budget: LevelBudget | None = None,
    thresholds: DecisionThresholds | None = None,
) -> SearchDecision:
    """Continue or stop. ``budget`` defaults to the level's row of ``LEVEL_BUDGETS``."""
    if not isinstance(state, SearchState):
        raise ValueError("state must be a SearchState")
    limits = budget if budget is not None else budget_for(state.level)
    gaps = find_gaps(state, thresholds)

    def stop(reason: DecisionReason) -> SearchDecision:
        return SearchDecision(Action.STOP, reason, gaps)

    if limits.max_queries == 0 or limits.max_pages == 0:
        return stop(DecisionReason.LEVEL_DOES_NOT_SEARCH)
    if state.elapsed_seconds >= limits.total_timeout_seconds:
        return stop(DecisionReason.TIME_BUDGET_EXHAUSTED)
    if state.pages_read >= limits.max_pages:
        return stop(DecisionReason.PAGE_BUDGET_EXHAUSTED)
    if state.executed_queries >= limits.max_queries:
        return stop(DecisionReason.QUERY_BUDGET_EXHAUSTED)
    if state.planned_queries > state.executed_queries:
        return SearchDecision(Action.CONTINUE, DecisionReason.PLANNED_QUERIES_REMAINING, ())
    if state.search_rounds_done >= limits.max_search_rounds:
        return stop(DecisionReason.SEARCH_ROUNDS_EXHAUSTED)
    if state.search_rounds_done > 0 and state.new_sources_last_round == 0:
        return stop(DecisionReason.NO_NEW_SOURCES)
    if gaps:
        return SearchDecision(Action.CONTINUE, _GAP_REASON[gaps[0]], ())
    return stop(DecisionReason.SUFFICIENT_COVERAGE)


def search_state_from_repository(
    repository: ResearchRepository,
    session_id: UUID,
    *,
    executed_queries: int,
    search_rounds_done: int,
    pages_read: int,
    elapsed_seconds: float,
    new_sources_last_round: int | None = None,
) -> SearchState:
    """Build a ``SearchState`` from stored data plus what only the running pipeline knows.

    Planned queries, source ratings and open conflicts come from the repository; the
    counters and the elapsed time are measured by the caller. Raises ``ValueError`` for an
    unknown session.
    """
    session = repository.get_session(session_id)
    if session is None:
        raise ValueError("research session does not exist")
    return SearchState(
        level=session.level,
        planned_queries=len(repository.list_queries(session_id)),
        executed_queries=executed_queries,
        search_rounds_done=search_rounds_done,
        pages_read=pages_read,
        elapsed_seconds=elapsed_seconds,
        evaluations=tuple(s.evaluation for s in repository.list_sources(session_id)),
        unresolved_conflicts=len(repository.list_conflicts(session_id, status=ConflictStatus.OPEN)),
        new_sources_last_round=new_sources_last_round,
    )
