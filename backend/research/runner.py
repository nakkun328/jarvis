"""Run a requested web research through the existing task queue.

One request is one research session plus one queue task. The task goal is a fixed label and
the session id (never the question), the steps are the five research stages, and the work is
done by ``QuickResearch`` or ``StandardResearch`` with injected parts: the budget-guarded
search provider, a page reader, and the application's chat provider.

Guarantees:

- At most one research is queued or running; a second request is refused (``Busy``).
- A request is refused before anything is queued when the local search budget is used up.
- A task completes only when a separate verifier finds the session ``completed`` AND either at
  least one stored, verified claim or the explicit "no verified claim" notice. Completion
  therefore never means that an answer was made up.
- A restart never re-runs interrupted work: the task and its session are marked failed.
- Failures are fixed codes. The question, queries, URLs, page text and vendor messages are
  never logged by this module (only exception types and counts).

The worker is one coroutine that claims one task at a time; it is started by the application
only when research is fully configured.
"""

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from backend.providers.base import CompletionRequest, CompletionResponse, LLMProvider
from backend.research.models import (
    FailureReason,
    ResearchLevel,
    ResearchSession,
    ResearchStatus,
)
from backend.research.quick import QuickLimits, QuickResearch
from backend.research.reader import FetchedPage
from backend.research.repository import (
    ResearchRepository,
    ResearchRepositoryError,
    ResearchStateChanged,
)
from backend.research.reuse import ReuseDecision, copy_prior_into, decide_reuse
from backend.research.run_control import (
    REQUESTABLE_LEVELS,
    BudgetExhausted,
    Busy,
    NotCancellable,
    UnknownSession,
)
from backend.research.search import SearchProvider, SearchQuery, SearchResult
from backend.research.standard import (
    CAVEAT_TEXT,
    Caveat,
    PageFetcher,
    ProgressEvent,
    Stage,
    StandardLimits,
    StandardResearch,
)
from backend.tasks.models import Task, TaskFailure, TaskStatus
from backend.tasks.queue import (
    CancellationToken,
    ExecutionOutcome,
    ProgressReporter,
    TaskNotCancellable,
    TaskQueue,
    VerificationResult,
)
from backend.tasks.repository import TaskRepositoryError

logger = logging.getLogger(__name__)

GOAL_LABEL = "web research"
STEP_DESCRIPTIONS: tuple[str, ...] = (
    "Plan the search queries",
    "Search the web",
    "Read the pages",
    "Verify the citations",
    "Write the result",
)
_STAGE_INDEX: Mapping[Stage, int] = {
    Stage.PLANNING: 0,
    Stage.SEARCHING: 1,
    Stage.READING: 2,
    Stage.VERIFYING: 3,
    Stage.WRITING: 4,
}
_GOAL = re.compile(
    re.escape(GOAL_LABEL) + r" ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

#: The Quick pipeline's fixed sentence for "the model found nothing it could cite".
QUICK_NO_CLAIM_NOTICE = "No claim could be verified against a source."
NO_VERIFIED_CLAIM_NOTICES = frozenset(
    {QUICK_NO_CLAIM_NOTICE, CAVEAT_TEXT[Caveat.NO_VERIFIED_CLAIMS]}
)

#: Longest a single task may run; the research budgets (120 s and 300 s) end a run sooner.
TASK_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_SECONDS = 0.5
_ACTIVE_STATUSES = (ResearchStatus.PENDING, ResearchStatus.RUNNING, ResearchStatus.WAITING)
_CANCEL_ATTEMPTS = 4
_CANCEL_RETRY_SECONDS = 0.05


def goal_for(session_id: UUID) -> str:
    return f"{GOAL_LABEL} {session_id}"


def session_id_from_goal(goal: str) -> UUID | None:
    """The session a task belongs to, or ``None`` when the goal is not ours."""
    match = _GOAL.fullmatch(goal)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:
        return None


def has_notice_of_no_verified_claims(result_text: str | None) -> bool:
    """True when the stored result states that no claim could be verified."""
    if not result_text:
        return False
    for line in result_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if stripped in NO_VERIFIED_CLAIM_NOTICES:
            return True
    return False


# ----- progress -----


@dataclass
class _Counters:
    stage: str = "planning"
    round_index: int = 0
    queries: int = 0
    pages: int = 0
    sources: int = 0
    claims: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "round": self.round_index,
            "queries": self.queries,
            "pages": self.pages,
            "sources": self.sources,
            "claims": self.claims,
        }


class _Steps:
    """Moves the task's step progress forward only, ignoring anything that would go back."""

    def __init__(self, reporter: ProgressReporter) -> None:
        self._reporter = reporter
        self._current: int | None = None

    def advance(self, index: int) -> None:
        if self._current is not None and index <= self._current:
            return
        try:
            if self._current is not None:
                self._reporter.finish_step(self._current)
            self._reporter.start_step(index)
            self._current = index
        except TaskRepositoryError as error:
            logger.info("research step update skipped type=%s", type(error).__name__)

    def finish(self) -> None:
        if self._current is None:
            return
        with suppress(TaskRepositoryError):
            self._reporter.finish_step(self._current)


class _CancelView:
    """Presents the queue's token under the name the Standard pipeline checks."""

    def __init__(self, token: CancellationToken) -> None:
        self._token = token

    @property
    def cancelled(self) -> bool:
        return self._token.is_set


class _QuickObserver:
    """Quick Research has no stage callback, so its collaborators report the stages."""

    def __init__(self, on_stage: Callable[[Stage], None], counters: _Counters) -> None:
        self._on_stage = on_stage
        self.counters = counters

    def enter(self, stage: Stage) -> None:
        self.counters.stage = stage.value
        self._on_stage(stage)


class _ObservedSearch:
    def __init__(self, inner: SearchProvider, observer: _QuickObserver) -> None:
        self._inner = inner
        self._observer = observer
        self.name = getattr(inner, "name", "search")

    async def search(self, query: SearchQuery) -> Sequence[SearchResult]:
        self._observer.counters.queries += 1
        self._observer.enter(Stage.SEARCHING)
        return await self._inner.search(query)


class _ObservedReader:
    def __init__(self, inner: PageFetcher, observer: _QuickObserver) -> None:
        self._inner = inner
        self._observer = observer

    async def read(self, url: str) -> FetchedPage:
        self._observer.counters.pages += 1
        self._observer.enter(Stage.READING)
        return await self._inner.read(url)


class _ObservedLLM:
    def __init__(self, inner: LLMProvider, observer: _QuickObserver) -> None:
        self._inner = inner
        self._observer = observer

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self._observer.enter(Stage.VERIFYING)
        response = await self._inner.complete(request)
        self._observer.enter(Stage.WRITING)
        return response

    def stream(self, request: CompletionRequest):  # pragma: no cover - research never streams
        return self._inner.stream(request)


# ----- the service -----


class ResearchRunService:
    """Submit, run, cancel and report research requests. Implements ``ResearchRuns``."""

    def __init__(
        self,
        repository: ResearchRepository,
        queue: TaskQueue,
        *,
        search: SearchProvider,
        reader: PageFetcher,
        llm: LLMProvider,
        is_budget_exhausted: Callable[[], bool] | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        quick_limits: QuickLimits | None = None,
        standard_limits: StandardLimits | None = None,
        clock: Callable[[], datetime] | None = None,
        on_completed: Callable[[UUID], None] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.repository = repository
        self.queue = queue
        self._search = search
        self._reader = reader
        self._llm = llm
        self._is_budget_exhausted = is_budget_exhausted
        self._poll = poll_seconds
        self._quick_limits = quick_limits
        self._standard_limits = standard_limits
        self._clock = clock or (lambda: datetime.now(UTC))
        # Called (off the event loop) with the id of a session that just completed with at least
        # one verified claim. A failure is logged and never changes the session or the task.
        self._on_completed = on_completed
        self._lock = threading.Lock()
        self._progress: dict[UUID, _Counters] = {}
        self._executor = _ResearchExecutor(self)
        self._verifier = _ResearchVerifier(self)

    # ----- requests (called from request threads) -----

    def submit(self, question: str, level: ResearchLevel) -> UUID:
        if level not in REQUESTABLE_LEVELS:
            raise ValueError("this research level is not available")
        with self._lock:
            if self._has_active_session():
                raise Busy
            if self._is_budget_exhausted is not None and self._is_budget_exhausted():
                raise BudgetExhausted
            session = self.repository.create_session(question, level)
            try:
                self.queue.submit(goal_for(session.id), STEP_DESCRIPTIONS)
            except Exception:
                self._close(session.id, cancel=True)
                raise
            return session.id

    def cancel(self, session_id: UUID) -> str:
        with self._lock:  # a request cannot slip in between the session and its task
            return self._cancel(session_id)

    def _cancel(self, session_id: UUID) -> str:
        session = self.repository.get_session(session_id)
        if session is None:
            raise UnknownSession
        if session.status in _TERMINAL:
            raise NotCancellable
        task = self._task_for(session_id)
        if task is None:
            # No task to stop (it was never queued, or it already ended): close the session.
            self._close(session_id, cancel=True)
            return "cancelled"
        for attempt in range(_CANCEL_ATTEMPTS):
            try:
                after = self.queue.cancel(task.id)
            except TaskNotCancellable:
                # The worker has claimed the task but not yet registered its token.
                if attempt + 1 < _CANCEL_ATTEMPTS:
                    time.sleep(_CANCEL_RETRY_SECONDS)
                    continue
                raise NotCancellable from None
            except TaskRepositoryError:
                raise NotCancellable from None
            if after.status is TaskStatus.CANCELLED:
                self._close(session_id, cancel=True)
                return "cancelled"
            return "cancelling"
        raise NotCancellable

    def progress(self, session_id: UUID) -> Mapping[str, object] | None:
        counters = self._progress.get(session_id)
        return None if counters is None else counters.as_dict()

    # ----- worker side (event loop) -----

    def recover(self) -> int:
        """Settle what a previous process left behind. Nothing is re-run.

        Running tasks become ``failed(interrupted)``. Research sessions that are not finished
        and have no pending task to run them are failed with ``internal_error`` (the stored
        failure codes have no "interrupted"; the Tasks screen shows the precise cause).
        """
        recovered = self.queue.repository.recover_in_flight()
        pending = {
            session_id
            for task in self.queue.repository.list_tasks(TaskStatus.PENDING, limit=100)
            if (session_id := session_id_from_goal(task.goal)) is not None
        }
        for status in _ACTIVE_STATUSES:
            for session in self.repository.list_sessions(status, limit=100):
                if status is ResearchStatus.PENDING and session.id in pending:
                    continue
                self._close(session.id, reason=FailureReason.INTERNAL_ERROR)
        return len(recovered)

    async def run_one(self) -> Task | None:
        """Claim and run the oldest pending task; ``None`` when nothing is pending."""
        task = await self.queue.run_next(self._executor, self._verifier)
        if task is not None:
            self._settle_session(task)
        return task

    async def run_worker(self, stop: asyncio.Event | None = None) -> None:
        """Run tasks one at a time until ``stop`` is set or the coroutine is cancelled."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            ran = None
            try:
                ran = await self.run_one()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("research worker iteration failed type=%s", type(error).__name__)
            if ran is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)

    # ----- pieces used by the executor and the verifier -----

    async def run_session(
        self,
        session: ResearchSession,
        reporter: ProgressReporter,
        token: CancellationToken,
    ) -> ResearchSession:
        counters = _Counters()
        self._progress[session.id] = counters
        steps = _Steps(reporter)
        steps.advance(0)
        reused = self._try_reuse(session, counters, steps)
        if reused is not None:
            return reused
        if session.level is ResearchLevel.STANDARD:

            def on_progress(event: ProgressEvent) -> None:
                counters.stage = event.stage.value
                counters.round_index = event.round_index
                counters.queries = event.queries_run
                counters.pages = event.pages_read
                counters.sources = event.sources
                counters.claims = event.verified_claims
                steps.advance(_STAGE_INDEX[event.stage])

            result = await StandardResearch(
                self._search, self._reader, self.repository, self._llm, self._standard_limits
            ).resume(session.id, cancel=_CancelView(token), on_progress=on_progress)
            steps.finish()
            return result.session

        def on_stage(stage: Stage) -> None:
            steps.advance(_STAGE_INDEX[stage])

        observer = _QuickObserver(on_stage, counters)
        quick = QuickResearch(
            _ObservedSearch(self._search, observer),
            _ObservedReader(self._reader, observer),  # type: ignore[arg-type]
            self.repository,
            _ObservedLLM(self._llm, observer),
            self._quick_limits,
        )
        result = await quick.resume(session.id)
        steps.finish()
        return result.session

    def _try_reuse(
        self, session: ResearchSession, counters: _Counters, steps: _Steps
    ) -> ResearchSession | None:
        """Decide about past research; complete from it when it is fresh enough.

        Returns the completed session when a verified prior result was reused, else ``None``
        and the run searches as usual. The decision is stored on the session either way.
        A stale or time-sensitive prior result is only linked as "previous result" context;
        it never becomes this run's result.
        """
        try:
            decision = decide_reuse(
                self.repository,
                session.question,
                session.level,
                self._clock(),
                exclude=session.id,
            )
            self.repository.set_reuse_decision(
                session.id,
                decision.reason.value,
                reuse_of=decision.prior.id if decision.prior else None,
                prior_at=decision.prior_at,
            )
        except (ResearchRepositoryError, ValueError) as error:
            logger.info("research reuse skipped type=%s", type(error).__name__)
            return None
        if not decision.reuse or decision.prior is None:
            return None
        # Past this point a storage error fails the run: a half-copied result must not
        # fall through to a normal search.
        return self._complete_from_prior(session, decision, counters, steps)

    def _complete_from_prior(
        self,
        session: ResearchSession,
        decision: ReuseDecision,
        counters: _Counters,
        steps: _Steps,
    ) -> ResearchSession | None:
        prior = decision.prior
        assert prior is not None and prior.result_text is not None
        current = self.repository.get_session(session.id)
        if current is None:
            return None
        if current.status is ResearchStatus.PENDING:
            self.repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
        counters.claims = copy_prior_into(self.repository, prior.id, session.id)
        counters.sources = len(self.repository.list_sources(session.id))
        counters.stage = Stage.WRITING.value
        steps.advance(_STAGE_INDEX[Stage.WRITING])
        finished = self.repository.set_result(session.id, prior.result_text)
        steps.finish()
        return finished

    # ----- helpers -----

    def _has_active_session(self) -> bool:
        return any(self.repository.list_sessions(status, limit=1) for status in _ACTIVE_STATUSES)

    def _task_for(self, session_id: UUID) -> Task | None:
        goal = goal_for(session_id)
        for status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            for task in self.queue.repository.list_tasks(status, limit=100):
                if task.goal == goal:
                    return task
        return None

    def _settle_session(self, task: Task) -> None:
        """Make the session agree with a task that ended without finishing it."""
        session_id = session_id_from_goal(task.goal)
        if session_id is None:
            return
        self._progress.pop(session_id, None)
        if task.status is TaskStatus.CANCELLED:
            self._close(session_id, cancel=True)
        elif task.status is TaskStatus.FAILED:
            reason = (
                FailureReason.TIMEOUT
                if task.failure_code is TaskFailure.TIMEOUT
                else FailureReason.INTERNAL_ERROR
            )
            self._close(session_id, reason=reason)

    def _close(
        self,
        session_id: UUID,
        *,
        cancel: bool = False,
        reason: FailureReason = FailureReason.INTERNAL_ERROR,
    ) -> None:
        """Move an unfinished session to cancelled or failed; a lost race is fine."""
        for _ in range(3):
            try:
                session = self.repository.get_session(session_id)
                if session is None or session.status in _TERMINAL:
                    return
                if cancel:
                    self.repository.transition(session_id, session.status, ResearchStatus.CANCELLED)
                    return
                if session.status is ResearchStatus.PENDING:
                    self.repository.transition(
                        session_id, ResearchStatus.PENDING, ResearchStatus.RUNNING
                    )
                    continue
                self.repository.transition(
                    session_id, session.status, ResearchStatus.FAILED, failure_reason=reason
                )
                return
            except ResearchStateChanged:
                continue
            except ResearchRepositoryError as error:
                logger.error("research session could not be closed type=%s", type(error).__name__)
                return


_TERMINAL = frozenset({ResearchStatus.FAILED, ResearchStatus.COMPLETED, ResearchStatus.CANCELLED})


class _ResearchExecutor:
    def __init__(self, service: ResearchRunService) -> None:
        self._service = service

    async def execute(
        self, task: Task, progress: ProgressReporter, cancellation: CancellationToken
    ) -> ExecutionOutcome:
        service = self._service
        session_id = session_id_from_goal(task.goal)
        if session_id is None:
            return ExecutionOutcome.failure()  # not a research task
        session = service.repository.get_session(session_id)
        if session is None or session.level not in REQUESTABLE_LEVELS:
            return ExecutionOutcome.failure()
        try:
            finished = await service.run_session(session, progress, cancellation)
        finally:
            service._progress.pop(session_id, None)
        if finished.status is not ResearchStatus.COMPLETED:
            return ExecutionOutcome.failure()
        claims = len(service.repository.list_claims(session_id))
        sources = len(service.repository.list_sources(session_id))
        if claims and service._on_completed is not None:
            try:
                await asyncio.to_thread(service._on_completed, session_id)
            except Exception as error:  # a hook failure must not fail a finished research
                logger.warning("completion hook failed type=%s", type(error).__name__)
        return ExecutionOutcome.succeeded(
            f"Research finished with {claims} verified claim(s) from {sources} source(s)."
        )


class _ResearchVerifier:
    """Passes only a completed session that cites verified claims or says it found none."""

    def __init__(self, service: ResearchRunService) -> None:
        self._service = service

    async def verify(self, task: Task, outcome: ExecutionOutcome) -> VerificationResult:
        repository = self._service.repository
        session_id = session_id_from_goal(task.goal)
        if session_id is None:
            return VerificationResult(False)
        session = repository.get_session(session_id)
        if (
            session is None
            or session.status is not ResearchStatus.COMPLETED
            or not (session.result_text or "").strip()
        ):
            return VerificationResult(False)
        if repository.list_claims(session_id):
            return VerificationResult(True)
        return VerificationResult(has_notice_of_no_verified_claims(session.result_text))
