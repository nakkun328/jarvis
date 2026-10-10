"""Standard Research (JAR-52): several searches, several sources, checked against each other.

Flow for one question (library only; nothing here is wired to a route or the task queue)::

    create session (standard) -> running
      first pass
        planning   plan queries (planner v2, no model)
        searching  search each query (bounded results)
        reading    read the best distinct URLs through the safe reader
        verifying  rate sources, ask the model for claims with quotes, verify every quote
                   (CitationManager), cross-check the sources, detect conflicts
      extra rounds, while ``decide_additional_search`` says continue and the budget allows
        the unmet gap -> follow-up queries (followup.py) -> the same four steps
      writing    result text from the verified claims only; open conflicts and unmet gaps
                 are listed as caveats -> completed

Everything fetched or returned by the search provider and the model is untrusted data. The
control path is fixed code: no text from a page, a title or a model reply can add a query,
a page, a round or a step. The only page-derived text that reaches a query is a few short
plain terms from source titles (see ``followup.py``).

Budget. The ceilings are the ``standard`` row of ``levels.LEVEL_BUDGETS`` (queries, results
per query, pages, extra rounds, wall-clock seconds); ``StandardLimits`` can only be set at or
below them. New rounds also stop at ``soft_deadline_fraction`` of the wall-clock budget so a
last round has time to finish; a hard ``asyncio`` timeout at the full budget ends the run as
``timeout``. A cancellation token is checked between every search, page read and step.

Fail closed. A provider, reader or model error in the FIRST pass ends the session ``failed``
with a fixed ``FailureReason`` and stores no result. In an extra round the same errors do not
discard the verified claims of the first pass; they become caveats in the result, so the
reader sees that the research is incomplete. A stop caused by the budget while gaps remain is
also a caveat. A cancelled run ends ``cancelled`` and stores no result.

What is verified and what is heuristic. Verified: every claim in the result has a quote that
occurs word for word (whitespace aside) in the text of the cited page. Heuristic: source
authority, freshness, relevance and agreement ratings, the conflict flags, and the "enough
sources" decision. None of those says a claim is true.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from backend.providers.base import ChatMessage, CompletionRequest, LLMProvider
from backend.research.citations import (
    CitationManager,
    DroppedClaim,
    DropReason,
    EvidenceSource,
    VerifiedClaim,
    has_twin,
    normalize_space,
    strip_unknown_urls,
)
from backend.research.conflicts import detect_and_store_conflicts
from backend.research.crosscheck import cross_check_and_store
from backend.research.domains import diversify, registrable_domain, split_blocked
from backend.research.evaluation import evaluate_and_store, relevance_rating
from backend.research.followup import FollowUpQuery, generate_follow_ups
from backend.research.levels import LEVEL_BUDGETS, LevelBudget
from backend.research.models import (
    MAX_RESULT_CHARS,
    ConflictKind,
    ConflictStatus,
    FailureReason,
    ResearchClaim,
    ResearchConflict,
    ResearchLevel,
    ResearchSession,
    ResearchSource,
    ResearchStatus,
)
from backend.research.normalizer import normalize_url
from backend.research.planner import DeterministicQueryPlanner, finalize_queries
from backend.research.quick import (
    SYSTEM_PROMPT,
    FailedRead,
    QueryPlanner,
    QuickLimits,
    ResearchFailed,
    build_user_message,
    clean_title,
    parse_response,
)
from backend.research.reader import FetchedPage, ReaderError
from backend.research.repository import (
    ResearchRepository,
    ResearchRepositoryError,
    ResearchStateChanged,
)
from backend.research.search import SearchError, SearchProvider, SearchQuery, SearchResult
from backend.research.search_decision import (
    Action,
    DecisionReason,
    DecisionThresholds,
    Gap,
    SearchState,
    decide_additional_search,
    find_gaps,
)

logger = logging.getLogger(__name__)

_STANDARD_BUDGET = LEVEL_BUDGETS[ResearchLevel.STANDARD]
RESULT_HEADER = (
    "Standard research result. Every statement below is backed by a quote found word for word "
    "in the cited page."
)
CITATION_NOTE = (
    "Note: a citation shows that the quote occurs in the page text that was read. It does not "
    "show that the claim is true. Source ratings and conflict flags are heuristics."
)
_MAX_CONFLICT_LINES = 10
_MAX_CLAIM_CHARS_IN_LINE = 200


class Stage(StrEnum):
    """Fixed progress stages, for a Task/Activity view."""

    PLANNING = "planning"
    SEARCHING = "searching"
    READING = "reading"
    VERIFYING = "verifying"
    WRITING = "writing"


@dataclass(frozen=True)
class ProgressEvent:
    """A progress report. Numbers and a stage code only: no query, URL or page text."""

    stage: Stage
    round_index: int  # 0 is the first pass, 1.. are the extra rounds
    queries_run: int
    pages_read: int  # pages tried so far, including failed ones
    sources: int
    verified_claims: int


ProgressCallback = Callable[[ProgressEvent], None]


class CancellationToken:
    """Set from anywhere to stop a run at its next checkpoint."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class PageFetcher(Protocol):
    """The reader contract: ``reader.PageReader`` or any object with the same ``read``."""

    async def read(self, url: str) -> FetchedPage: ...


class Caveat(StrEnum):
    """Fixed codes for the limits of a finished result; each has a fixed sentence."""

    NO_SOURCES = "no_sources"
    FEW_RELEVANT_SOURCES = "few_relevant_sources"
    UNRESOLVED_CONFLICTS = "unresolved_conflicts"
    NO_AUTHORITATIVE_SOURCE = "no_authoritative_source"
    STALE_SOURCES = "stale_sources"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    PAGE_BUDGET_EXHAUSTED = "page_budget_exhausted"
    QUERY_BUDGET_EXHAUSTED = "query_budget_exhausted"
    SEARCH_ROUNDS_EXHAUSTED = "search_rounds_exhausted"
    NO_NEW_SOURCES = "no_new_sources"
    NO_FOLLOW_UP_QUERY = "no_follow_up_query"
    SEARCH_PARTIAL = "search_partial"
    FOLLOW_UP_SEARCH_FAILED = "follow_up_search_failed"
    FOLLOW_UP_INCOMPLETE = "follow_up_incomplete"
    READS_FAILED = "reads_failed"
    CLAIMS_REMOVED = "claims_removed"
    NO_VERIFIED_CLAIMS = "no_verified_claims"
    SINGLE_DOMAIN = "single_domain"
    SAME_SITE_SOURCES = "same_site_sources"


CAVEAT_TEXT: dict[Caveat, str] = {
    Caveat.NO_SOURCES: "No source could be read.",
    Caveat.FEW_RELEVANT_SOURCES: "Fewer relevant sources were found than this level aims for.",
    Caveat.UNRESOLVED_CONFLICTS: "Some sources disagree and the disagreement is not resolved.",
    Caveat.NO_AUTHORITATIVE_SOURCE: "No source from an authoritative kind of site was found.",
    Caveat.STALE_SOURCES: "Every relevant source with a known date is old.",
    Caveat.TIME_BUDGET_EXHAUSTED: "The search stopped because the time budget was used up.",
    Caveat.PAGE_BUDGET_EXHAUSTED: "The search stopped because the page budget was used up.",
    Caveat.QUERY_BUDGET_EXHAUSTED: "The search stopped because the query budget was used up.",
    Caveat.SEARCH_ROUNDS_EXHAUSTED: "The search stopped because the extra rounds were used up.",
    Caveat.NO_NEW_SOURCES: "The last extra round found no new source, so the search stopped.",
    Caveat.NO_FOLLOW_UP_QUERY: "No new follow-up query could be built for the open gaps.",
    Caveat.SEARCH_PARTIAL: "Some searches of the first pass failed.",
    Caveat.FOLLOW_UP_SEARCH_FAILED: "The searches of an extra round failed.",
    Caveat.FOLLOW_UP_INCOMPLETE: "An extra round could not be completed; its sources are unused.",
    Caveat.READS_FAILED: "Some pages could not be read.",
    Caveat.CLAIMS_REMOVED: "Some proposed claims were removed because they could not be verified.",
    Caveat.NO_VERIFIED_CLAIMS: "The sources did not support any claim that could be verified.",
    Caveat.SINGLE_DOMAIN: "All verified claims come from one website, so they are not independent.",
    Caveat.SAME_SITE_SOURCES: (
        "Some listed sources are different pages of the same website, so they are not "
        "independent confirmation of each other."
    ),
}

_GAP_CAVEAT = {
    Gap.NO_SOURCES: Caveat.NO_SOURCES,
    Gap.FEW_RELEVANT_SOURCES: Caveat.FEW_RELEVANT_SOURCES,
    Gap.UNRESOLVED_CONFLICTS: Caveat.UNRESOLVED_CONFLICTS,
    Gap.NO_AUTHORITATIVE_SOURCE: Caveat.NO_AUTHORITATIVE_SOURCE,
    Gap.STALE_SOURCES: Caveat.STALE_SOURCES,
}
_STOP_CAVEAT = {
    DecisionReason.TIME_BUDGET_EXHAUSTED: Caveat.TIME_BUDGET_EXHAUSTED,
    DecisionReason.PAGE_BUDGET_EXHAUSTED: Caveat.PAGE_BUDGET_EXHAUSTED,
    DecisionReason.QUERY_BUDGET_EXHAUSTED: Caveat.QUERY_BUDGET_EXHAUSTED,
    DecisionReason.SEARCH_ROUNDS_EXHAUSTED: Caveat.SEARCH_ROUNDS_EXHAUSTED,
    DecisionReason.NO_NEW_SOURCES: Caveat.NO_NEW_SOURCES,
}
_KIND_LABEL = {
    ConflictKind.NUMBER_MISMATCH: "different figures",
    ConflictKind.DATE_MISMATCH: "different dates",
    ConflictKind.NEGATION_MISMATCH: "one says the opposite",
}


@dataclass(frozen=True)
class StandardLimits:
    """Work limits of one run. Defaults are the ``standard`` budget row; none may exceed it."""

    max_queries: int = _STANDARD_BUDGET.max_queries
    results_per_query: int = _STANDARD_BUDGET.results_per_query
    max_pages: int = _STANDARD_BUDGET.max_pages
    max_search_rounds: int = _STANDARD_BUDGET.max_search_rounds
    total_timeout: float = _STANDARD_BUDGET.total_timeout_seconds
    initial_queries: int = 3
    follow_ups_per_round: int = 1
    first_pass_pages: int = 5  # the rest of ``max_pages`` is kept for extra rounds
    pages_per_round: int = 2
    read_concurrency: int = 2
    search_timeout: float = 15.0
    page_timeout: float = 20.0
    llm_timeout: float = 60.0
    soft_deadline_fraction: float = 0.8
    max_chars_per_source: int = 4000
    max_total_evidence_chars: int = 12000
    max_response_chars: int = 20000
    max_claims_per_round: int = 10
    max_total_claims: int = 20

    def __post_init__(self) -> None:
        counts = (
            "max_queries",
            "results_per_query",
            "max_pages",
            "max_search_rounds",
            "initial_queries",
            "follow_ups_per_round",
            "first_pass_pages",
            "pages_per_round",
            "read_concurrency",
            "max_chars_per_source",
            "max_total_evidence_chars",
            "max_response_chars",
            "max_claims_per_round",
            "max_total_claims",
        )
        for name in counts:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("search_timeout", "page_timeout", "llm_timeout", "total_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise ValueError(f"{name} must be a positive number of seconds")
        fraction = self.soft_deadline_fraction
        if isinstance(fraction, bool) or not isinstance(fraction, int | float):
            raise ValueError("soft_deadline_fraction must be a number")
        if not 0 < fraction <= 1:
            raise ValueError("soft_deadline_fraction must be above 0 and at most 1")
        ceilings = {
            "max_queries": _STANDARD_BUDGET.max_queries,
            "results_per_query": _STANDARD_BUDGET.results_per_query,
            "max_pages": _STANDARD_BUDGET.max_pages,
            "max_search_rounds": _STANDARD_BUDGET.max_search_rounds,
            "total_timeout": _STANDARD_BUDGET.total_timeout_seconds,
            "read_concurrency": 2,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise ValueError(f"{name} must be at most {ceiling} at the standard level")
        if self.initial_queries > self.max_queries:
            raise ValueError("initial_queries must not exceed max_queries")
        if self.first_pass_pages > self.max_pages:
            raise ValueError("first_pass_pages must not exceed max_pages")

    def level_budget(self) -> LevelBudget:
        """The budget handed to the decision: the soft wall-clock deadline, not the hard one."""
        return LevelBudget(
            ResearchLevel.STANDARD,
            self.max_queries,
            self.results_per_query,
            self.max_pages,
            self.max_search_rounds,
            self.total_timeout * self.soft_deadline_fraction,
        )

    def evidence_limits(self) -> QuickLimits:
        return QuickLimits(
            max_chars_per_source=self.max_chars_per_source,
            max_total_evidence_chars=self.max_total_evidence_chars,
            max_response_chars=self.max_response_chars,
            llm_timeout=self.llm_timeout,
        )


@dataclass(frozen=True)
class StandardResult:
    session: ResearchSession
    queries: tuple[str, ...] = ()
    sources: tuple[ResearchSource, ...] = ()
    claims: tuple[ResearchClaim, ...] = ()
    conflicts: tuple[ResearchConflict, ...] = ()
    dropped_claims: tuple[DroppedClaim, ...] = ()
    failed_reads: tuple[FailedRead, ...] = ()
    follow_ups: tuple[FollowUpQuery, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    stop_reason: DecisionReason | None = None
    search_rounds: int = 0  # extra rounds after the first pass
    pages_tried: int = 0


class _Cancelled(Exception):
    """Internal control flow: the cancellation token was set."""


class _Run:
    """Per-call scratch data. Never shared between runs, so concurrent sessions stay isolated."""

    def __init__(
        self,
        session: ResearchSession,
        cancel: CancellationToken | None,
        on_progress: ProgressCallback | None,
        started: float,
    ) -> None:
        self.session = session
        self.cancel = cancel
        self.on_progress = on_progress
        self.started = started
        self.round_index = 0
        self.queries: list[str] = []
        self.attempted: set[str] = set()  # normalised URLs already tried
        self.pages_tried = 0
        self.evidence: list[EvidenceSource] = []
        self.texts: dict[UUID, str] = {}
        self.verified: list[VerifiedClaim] = []
        self.dropped: list[DroppedClaim] = []
        self.failed_reads: list[FailedRead] = []
        self.follow_ups: list[FollowUpQuery] = []
        self.caveats: list[Caveat] = []
        self.insufficient_rounds = 0
        self.extraction_rounds = 0
        self.stop_reason: DecisionReason | None = None
        self.rounds_done = 0
        self.new_sources_last_round: int | None = None
        self.stop_loop = False

    def note(self, caveat: Caveat) -> None:
        if caveat not in self.caveats:
            self.caveats.append(caveat)


class StandardResearch:
    def __init__(
        self,
        search: SearchProvider,
        reader: PageFetcher,
        repository: ResearchRepository,
        llm: LLMProvider,
        limits: StandardLimits | None = None,
        *,
        planner: QueryPlanner | None = None,
        thresholds: DecisionThresholds | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._search = search
        self._reader = reader
        self._repository = repository
        self._llm = llm
        self._limits = limits or StandardLimits()
        self._planner = planner or DeterministicQueryPlanner()
        self._thresholds = thresholds
        self._clock = clock or time.monotonic
        self._evidence_limits = self._limits.evidence_limits()

    async def run(
        self,
        question: str,
        *,
        cancel: CancellationToken | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> StandardResult:
        """Research one question. Raises ValueError for an invalid question (no session)."""
        session = self._repository.create_session(question, ResearchLevel.STANDARD)
        return await self.resume(session.id, cancel=cancel, on_progress=on_progress)

    async def resume(
        self,
        session_id: UUID,
        *,
        cancel: CancellationToken | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> StandardResult:
        """Run or continue a session. Finished sessions are returned unchanged.

        Re-entry is safe: queries, sources and claims are not stored twice. Raises
        ``ValueError`` for an unknown session or one that is not at the standard level.
        """
        session = self._repository.get_session(session_id)
        if session is None:
            raise ValueError("unknown research session")
        if session.level is not ResearchLevel.STANDARD:
            raise ValueError("research session is not at the standard level")
        if session.status in (ResearchStatus.PENDING, ResearchStatus.WAITING):
            try:
                session = self._repository.transition(
                    session.id, session.status, ResearchStatus.RUNNING
                )
            except ResearchStateChanged:
                return self._snapshot(session_id)
        if session.status is not ResearchStatus.RUNNING:
            return self._snapshot(session_id)
        run = _Run(session, cancel, on_progress, self._clock())
        try:
            async with asyncio.timeout(self._limits.total_timeout):
                await self._execute(run)
        except asyncio.CancelledError:
            self._finish(session_id, cancel=True)
            raise
        except _Cancelled:
            self._finish(session_id, cancel=True)
        except TimeoutError:
            self._finish(session_id, FailureReason.TIMEOUT)
        except ResearchFailed as failure:
            self._finish(session_id, failure.reason)
        except ResearchStateChanged:
            pass  # another worker finished the session first
        except ResearchRepositoryError:
            self._finish(session_id, FailureReason.INTERNAL_ERROR)
            raise
        except Exception as error:
            logger.error("standard research internal error type=%s", type(error).__name__)
            self._finish(session_id, FailureReason.INTERNAL_ERROR)
        return self._snapshot(session_id, run)

    # ----- pipeline -----

    async def _execute(self, run: _Run) -> None:
        limits = self._limits
        session = run.session
        self._emit(run, Stage.PLANNING)
        planned = finalize_queries(
            self._planner.plan(session.question, limits.initial_queries), limits.initial_queries
        )
        if not planned:
            raise ResearchFailed(FailureReason.INTERNAL_ERROR)
        await self._round(run, planned, limits.first_pass_pages, first_pass=True)

        budget = limits.level_budget()
        while True:
            self._checkpoint(run)
            state = self._search_state(run)
            decision = decide_additional_search(state, budget, self._thresholds)
            run.stop_reason = decision.reason
            if decision.action is Action.STOP or run.stop_loop:
                break
            remaining_queries = limits.max_queries - len(run.queries)
            titles = [s.title for s in self._repository.list_sources(session.id) if s.title]
            follow_ups = generate_follow_ups(
                session.question,
                find_gaps(state, self._thresholds),
                existing_queries=run.queries,
                source_titles=titles,
                max_queries=min(limits.follow_ups_per_round, remaining_queries),
            )
            if not follow_ups:
                run.note(Caveat.NO_FOLLOW_UP_QUERY)
                break
            run.rounds_done += 1
            run.round_index = run.rounds_done
            run.follow_ups.extend(follow_ups)
            pages_left = limits.max_pages - run.pages_tried
            await self._round(
                run,
                [item.text for item in follow_ups],
                min(limits.pages_per_round, pages_left),
                first_pass=False,
            )

        self._checkpoint(run)
        self._write(run)

    async def _round(
        self, run: _Run, queries: Sequence[str], page_cap: int, *, first_pass: bool
    ) -> None:
        """One search, read and verify pass. The first pass raises; later rounds add caveats."""
        before = len(run.evidence)
        try:
            hits, failures = await self._search_queries(run, queries)
            if failures == len(queries):
                raise ResearchFailed(FailureReason.SEARCH_FAILED)
            if failures:
                run.note(Caveat.SEARCH_PARTIAL if first_pass else Caveat.FOLLOW_UP_SEARCH_FAILED)
            candidates = self._candidates(run, hits)
            if first_pass and not candidates:
                raise ResearchFailed(FailureReason.NO_RESULTS)
            fresh = await self._read_and_store(run, candidates[:page_cap])
            if first_pass and not run.evidence:
                raise ResearchFailed(FailureReason.READER_FAILED)
            if fresh:
                await self._extract(run, fresh)
        except ResearchFailed as failure:
            if first_pass:
                raise
            if failure.reason is FailureReason.SEARCH_FAILED:
                run.note(Caveat.FOLLOW_UP_SEARCH_FAILED)
            else:
                run.note(Caveat.FOLLOW_UP_INCOMPLETE)
                run.stop_loop = True
        run.new_sources_last_round = None if first_pass else len(run.evidence) - before

    async def _search_queries(
        self, run: _Run, queries: Sequence[str]
    ) -> tuple[list[SearchResult], int]:
        limits = self._limits
        self._emit(run, Stage.SEARCHING)
        known = {q.text for q in self._repository.list_queries(run.session.id)}
        hits: list[SearchResult] = []
        failures = 0
        for text in queries:
            self._checkpoint(run)
            if text not in known:
                self._repository.add_query(run.session.id, text)
            run.queries.append(text)  # a query counts as run even when it fails
            try:
                query = SearchQuery(text, max_results=limits.results_per_query)
                async with asyncio.timeout(limits.search_timeout):
                    found = await self._search.search(query)
            except (SearchError, TimeoutError, ValueError) as error:
                failures += 1
                logger.info("research search failed type=%s", type(error).__name__)
                continue
            # Blocked URLs are set aside first so they take neither a result slot nor a page.
            usable = self._set_blocked_aside(run, list(found))
            hits.extend(usable[: limits.results_per_query])
        return hits, failures

    @staticmethod
    def _set_blocked_aside(run: _Run, hits: Sequence[SearchResult]) -> list[SearchResult]:
        readable, blocked = split_blocked(hits)
        for hit, reason in blocked:
            key = _url_key(hit.url)
            if key not in run.attempted:
                run.attempted.add(key)  # recorded once; never counted as a page tried
                run.failed_reads.append(FailedRead(hit.url, reason.value))
                run.note(Caveat.READS_FAILED)
        return readable

    @staticmethod
    def _candidates(run: _Run, hits: Sequence[SearchResult]) -> list[SearchResult]:
        """Distinct, not yet tried, fetchable URLs; best rank first, one page per domain first.

        Blocked URLs (internal addresses, bad schemes) never get here: the search step records
        them as failed reads and they take no slot and do not count as pages tried. The best hit
        of each registrable domain comes before a second hit of the same domain, so a page
        limit is not filled by one site while other domains are available. Hits whose title,
        snippet and URL clearly share none of the question's terms (``hit_prescore`` below
        ``PRESCORE_FLOOR``) go after the others, so they are read only when pages are left over.
        """
        ordered = sorted(enumerate(hits), key=lambda pair: (pair[1].rank, pair[0]))
        distinct: list[SearchResult] = []
        seen: set[str] = set()
        for _, hit in ordered:
            key = _url_key(hit.url)
            if key in run.attempted or key in seen:
                continue
            seen.add(key)
            distinct.append(hit)
        readable, _ = split_blocked(distinct)  # already recorded by the search step
        ordered_hits = diversify(readable)
        queries = run.queries
        question = run.session.question
        scored = [(_prescore(question, queries, hit), hit) for hit in ordered_hits]
        relevant = [hit for score, hit in scored if score is None or score >= PRESCORE_FLOOR]
        irrelevant = [hit for score, hit in scored if score is not None and score < PRESCORE_FLOOR]
        return relevant + irrelevant

    async def _read_and_store(
        self, run: _Run, candidates: Sequence[SearchResult]
    ) -> list[EvidenceSource]:
        """Read pages (bounded concurrency), store and rate the new sources."""
        self._emit(run, Stage.READING)
        for hit in candidates:
            run.attempted.add(_url_key(hit.url))
        run.pages_tried += len(candidates)
        outcomes = await self._read_pages(run, candidates)
        fresh: list[EvidenceSource] = []
        known_ids = {item.source.id for item in run.evidence}
        seen_finals = {_final_key(item.source.final_url) for item in run.evidence}
        stored: list[tuple[ResearchSource, FetchedPage]] = []
        for hit, outcome in zip(candidates, outcomes, strict=True):
            if isinstance(outcome, str):
                run.failed_reads.append(FailedRead(hit.url, outcome))
                continue
            if _final_key(outcome.final_url) in seen_finals:
                continue  # the same final page (fragment, trailing slash, utm_*) is one source
            source = self._repository.add_source(
                run.session.id,
                url=hit.url,
                final_url=outcome.final_url,
                retrieved_at=outcome.retrieved_at,
                content_digest=outcome.body_sha256,
                title=clean_title(outcome.title) or clean_title(hit.title),
                published_at=outcome.published_at or hit.published_at,
                source_type=hit.source_type,
            )
            if source.id in known_ids:
                continue  # the same page under another URL
            known_ids.add(source.id)
            seen_finals.add(_final_key(outcome.final_url))
            stored.append((source, outcome))
        # One numbering everywhere: the number a source has in the result text is its position in
        # the stored source list (the order the detail screen shows), not the order the reads
        # happened to finish in.
        position = {item.id: index for index, item in enumerate(self._list_sources(run))}
        stored.sort(key=lambda pair: position.get(pair[0].id, len(position)))
        for source, outcome in stored:
            rated = evaluate_and_store(
                self._repository,
                source,
                run.session.question,
                text=outcome.text,
                queries=run.queries,
            )
            item = EvidenceSource(len(run.evidence) + 1, rated, outcome.text)
            run.evidence.append(item)
            run.texts[rated.id] = outcome.text
            fresh.append(item)
        if any(isinstance(o, str) for o in outcomes):
            run.note(Caveat.READS_FAILED)
        return fresh

    def _list_sources(self, run: _Run) -> list[ResearchSource]:
        return self._repository.list_sources(run.session.id)

    async def _read_pages(
        self, run: _Run, candidates: Sequence[SearchResult]
    ) -> list[FetchedPage | str]:
        semaphore = asyncio.Semaphore(self._limits.read_concurrency)
        outcomes: list[FetchedPage | str] = ["cancelled"] * len(candidates)

        async def read(position: int, url: str) -> None:
            async with semaphore:
                if run.cancel is not None and run.cancel.cancelled:
                    return  # the checkpoint after the group ends the run
                try:
                    async with asyncio.timeout(self._limits.page_timeout):
                        page = await self._reader.read(url)
                    outcomes[position] = page if page.text.strip() else "empty_text"
                except ReaderError as error:
                    outcomes[position] = error.reason.value
                except TimeoutError:
                    outcomes[position] = "timeout"
                except Exception as error:
                    logger.info("research read failed type=%s", type(error).__name__)
                    outcomes[position] = "network_error"

        async with asyncio.TaskGroup() as group:
            for position, hit in enumerate(candidates):
                group.create_task(read(position, hit.url))
        self._checkpoint(run)
        return outcomes

    async def _extract(self, run: _Run, fresh: Sequence[EvidenceSource]) -> None:
        """Ask the model about the new sources, keep only verified claims, then cross-check."""
        self._checkpoint(run)
        self._emit(run, Stage.VERIFYING)
        limits = self._limits
        raw = await self._ask_llm(run.session.question, fresh)
        # The model's free-text answer is dropped: the result is built from verified claims only.
        _answer, insufficient, proposed = parse_response(raw)
        run.extraction_rounds += 1
        room = limits.max_total_claims - len(run.verified)
        manager = CitationManager(self._repository, run.session.id, run.evidence)
        report = manager.verify(proposed, max_claims=min(limits.max_claims_per_round, max(room, 0)))
        run.dropped.extend(report.dropped)
        only_absence = any(d.reason is DropReason.ABSENCE_STATEMENT for d in report.dropped)
        if (insufficient or only_absence) and not report.verified:
            # A "claim" that only says information is missing counts as the model saying the
            # sources fall short; it never becomes a verified claim.
            run.insufficient_rounds += 1
        kept: list[VerifiedClaim] = []
        for claim in report.verified:
            if not _already(run.verified, claim) and not has_twin([*run.verified, *kept], claim):
                kept.append(claim)
        manager.persist(kept)
        run.verified.extend(kept)
        if run.verified:
            cross_check_and_store(self._repository, run.session.id, run.texts)
            detect_and_store_conflicts(self._repository, run.session.id, run.texts)

    async def _ask_llm(self, question: str, evidence: Sequence[EvidenceSource]) -> str:
        request = CompletionRequest(
            (
                ChatMessage("system", SYSTEM_PROMPT),
                ChatMessage("user", build_user_message(question, evidence, self._evidence_limits)),
            )
        )
        try:
            async with asyncio.timeout(self._limits.llm_timeout):
                response = await self._llm.complete(request)
        except TimeoutError:
            raise ResearchFailed(FailureReason.TIMEOUT) from None
        except Exception as error:
            logger.info("research synthesis failed type=%s", type(error).__name__)
            raise ResearchFailed(FailureReason.SYNTHESIS_FAILED) from None
        text = response.text if isinstance(response.text, str) else ""
        if len(text) > self._limits.max_response_chars:
            raise ResearchFailed(FailureReason.BUDGET_EXCEEDED)
        return text

    # ----- decision state -----

    def _search_state(self, run: _Run) -> SearchState:
        sources = self._repository.list_sources(run.session.id)
        open_conflicts = self._repository.list_conflicts(run.session.id, status=ConflictStatus.OPEN)
        return SearchState(
            level=ResearchLevel.STANDARD,
            planned_queries=len(run.queries),  # every query is run as soon as it is planned
            executed_queries=len(run.queries),
            search_rounds_done=run.rounds_done,
            pages_read=run.pages_tried,
            elapsed_seconds=max(self._clock() - run.started, 0.0),
            evaluations=tuple(source.evaluation for source in sources),
            unresolved_conflicts=len(open_conflicts),
            new_sources_last_round=run.new_sources_last_round,
        )

    # ----- result text -----

    def _write(self, run: _Run) -> None:
        self._emit(run, Stage.WRITING)
        session = run.session
        if not run.verified and (
            run.extraction_rounds == 0 or run.insufficient_rounds < run.extraction_rounds
        ):
            # No claim survived verification and the model did not say the sources fall short.
            raise ResearchFailed(FailureReason.SYNTHESIS_FAILED)
        state = self._search_state(run)
        gaps = find_gaps(state, self._thresholds)
        conflict_lines, conflict_sources = self._conflict_lines(run)
        caveats = self._caveats(run, gaps, conflict_sources)

        manager = CitationManager(self._repository, session.id, run.evidence)
        # Every source number used anywhere in the text (claims and conflicts) is listed.
        lines = [manager.render(RESULT_HEADER, run.verified, also_cited=conflict_sources)]
        if conflict_lines:
            lines += [
                "",
                "Open conflicts (the sources disagree; nothing here decides who is right):",
            ]
            lines += conflict_lines
        if caveats:
            lines += ["", "Caveats:"]
            lines += [f"- {self._caveat_line(run, caveat)}" for caveat in caveats]
        lines += ["", CITATION_NOTE]
        text = "\n".join(lines)
        if len(text) > MAX_RESULT_CHARS:
            raise ResearchFailed(FailureReason.BUDGET_EXCEEDED)
        run.caveats = list(caveats)
        self._repository.set_result(session.id, text)

    def _caveats(
        self, run: _Run, gaps: Sequence[Gap], conflict_sources: Collection[int] = ()
    ) -> list[Caveat]:
        caveats: list[Caveat] = []
        if not run.verified:
            caveats.append(Caveat.NO_VERIFIED_CLAIMS)
        caveats += [_GAP_CAVEAT[gap] for gap in gaps]
        stop = _STOP_CAVEAT.get(run.stop_reason) if run.stop_reason else None
        if stop is not None and gaps:
            caveats.append(stop)
        if self._single_domain(run):
            caveats.append(Caveat.SINGLE_DOMAIN)
        elif self._same_site_listed(run, conflict_sources):
            caveats.append(Caveat.SAME_SITE_SOURCES)
        for caveat in run.caveats:
            if caveat not in caveats:
                caveats.append(caveat)
        if run.dropped and Caveat.CLAIMS_REMOVED not in caveats:
            caveats.append(Caveat.CLAIMS_REMOVED)
        return caveats

    @staticmethod
    def _single_domain(run: _Run) -> bool:
        """Two or more sources carry the verified claims, yet all share one registrable domain."""
        by_index = {item.index: item.source for item in run.evidence}
        cited = {claim.source_index for claim in run.verified}
        domains = {registrable_domain(by_index[i].final_url) for i in cited if i in by_index}
        return len(cited) >= 2 and len(domains) == 1 and None not in domains

    @staticmethod
    def _same_site_listed(run: _Run, conflict_sources: Collection[int]) -> bool:
        """Two of the listed sources (claims and conflicts) are pages of one registrable domain."""
        by_index = {item.index: item.source for item in run.evidence}
        listed = {claim.source_index for claim in run.verified} | set(conflict_sources)
        domains = [
            registrable_domain(by_index[i].final_url) for i in sorted(listed) if i in by_index
        ]
        known = [d for d in domains if d is not None]
        return len(known) != len(set(known))

    @staticmethod
    def _caveat_line(run: _Run, caveat: Caveat) -> str:
        text = CAVEAT_TEXT[caveat]
        if caveat is Caveat.READS_FAILED:
            return f"{len(run.failed_reads)} page(s) could not be read. {text}"
        if caveat is Caveat.CLAIMS_REMOVED:
            return f"{len(run.dropped)} claim(s) removed. {text}"
        return text

    def _conflict_lines(self, run: _Run) -> tuple[list[str], set[int]]:
        """Lines for the open conflicts and the source numbers they refer to."""
        session_id = run.session.id
        claims = {claim.id: claim for claim in self._repository.list_claims(session_id)}
        index_of = {item.source.id: item.index for item in run.evidence}
        allowed = {url for item in run.evidence for url in (item.source.url, item.source.final_url)}
        open_conflicts = self._repository.list_conflicts(session_id, status=ConflictStatus.OPEN)

        def quoted(claim_id: UUID | None) -> str:
            claim = claims.get(claim_id) if claim_id is not None else None
            if claim is None:
                return '"(claim)"'
            shown = normalize_space(strip_unknown_urls(claim.claim_text, allowed))
            return f'"{shown[:_MAX_CLAIM_CHARS_IN_LINE]}"'

        lines: list[str] = []
        referenced: set[int] = set()
        for conflict in open_conflicts[:_MAX_CONFLICT_LINES]:
            referenced.update(
                index_of[source_id]
                for source_id in (conflict.source_a_id, conflict.source_b_id)
                if source_id in index_of
            )
            label = _KIND_LABEL[conflict.kind]
            a = f"{quoted(conflict.claim_a_id)} [{index_of.get(conflict.source_a_id, '?')}]"
            if conflict.claim_b_id is not None:
                b = f"{quoted(conflict.claim_b_id)} [{index_of.get(conflict.source_b_id, '?')}]"
                lines.append(f"- {label}: {a} against {b}")
            else:
                other = index_of.get(conflict.source_b_id, "?")
                lines.append(f"- {label}: {a} against the text of source [{other}]")
        if len(open_conflicts) > _MAX_CONFLICT_LINES:
            lines.append(f"- and {len(open_conflicts) - _MAX_CONFLICT_LINES} more")
        return lines, referenced

    # ----- helpers -----

    def _checkpoint(self, run: _Run) -> None:
        if run.cancel is not None and run.cancel.cancelled:
            raise _Cancelled

    def _emit(self, run: _Run, stage: Stage) -> None:
        if run.on_progress is None:
            return
        event = ProgressEvent(
            stage,
            run.round_index,
            len(run.queries),
            run.pages_tried,
            len(run.evidence),
            len(run.verified),
        )
        try:
            run.on_progress(event)
        except Exception as error:  # a broken progress view must not break the research
            logger.info("research progress callback failed type=%s", type(error).__name__)

    def _finish(
        self, session_id: UUID, reason: FailureReason | None = None, *, cancel: bool = False
    ) -> None:
        """Move a running/waiting session to failed or cancelled; a lost race is fine."""
        for _ in range(2):
            session = self._repository.get_session(session_id)
            if session is None or session.status in (
                ResearchStatus.FAILED,
                ResearchStatus.COMPLETED,
                ResearchStatus.CANCELLED,
            ):
                return
            try:
                if cancel:
                    self._repository.transition(
                        session_id, session.status, ResearchStatus.CANCELLED
                    )
                else:
                    self._repository.transition(
                        session_id, session.status, ResearchStatus.FAILED, failure_reason=reason
                    )
                return
            except ResearchStateChanged:
                continue
            except ResearchRepositoryError:
                logger.error("research could not record final state")
                return

    def _snapshot(self, session_id: UUID, run: _Run | None = None) -> StandardResult:
        session = self._repository.get_session(session_id)
        assert session is not None
        stored = {source.id: source for source in self._repository.list_sources(session_id)}
        # Evidence order (search rank) when this call read pages; stored order is arbitrary.
        ordered = [item.source.id for item in run.evidence] if run else []
        sources = tuple(stored[i] for i in ordered if i in stored) or tuple(stored.values())
        done = session.status is ResearchStatus.COMPLETED
        return StandardResult(
            session=session,
            queries=tuple(q.text for q in self._repository.list_queries(session_id)),
            sources=sources,
            claims=tuple(self._repository.list_claims(session_id)),
            conflicts=tuple(self._repository.list_conflicts(session_id)),
            dropped_claims=tuple(run.dropped) if run else (),
            failed_reads=tuple(run.failed_reads) if run else (),
            follow_ups=tuple(run.follow_ups) if run else (),
            caveats=tuple(run.caveats) if run and done else (),
            stop_reason=run.stop_reason if run else None,
            search_rounds=run.rounds_done if run else 0,
            pages_tried=run.pages_tried if run else 0,
        )


PRESCORE_FLOOR = 0.2  # below this a hit's title/snippet/URL shares nothing with the question


def _prescore(question: str, queries: Sequence[str], hit: SearchResult) -> float | None:
    """Relevance of a search hit from its title, snippet and URL alone (``None``: no text)."""
    value, _ = relevance_rating(
        question, hit.title, hit.snippet, url=hit.url, queries=queries
    )
    return value


def _url_key(url: str) -> str:
    """Identity of a result URL for 'read once': the normalised URL, else the text itself."""
    try:
        return normalize_url(url)
    except ValueError:
        return url


def _already(verified: Sequence[VerifiedClaim], claim: VerifiedClaim) -> bool:
    return any(
        (v.text, v.source_id, v.quote) == (claim.text, claim.source_id, claim.quote)
        for v in verified
    )


def _final_key(url: str) -> str:
    """Identity of a final URL for 'one source per page': normalised, no trailing slash."""
    base, mark, query = _url_key(url).partition("?")
    return base.rstrip("/") + mark + query
