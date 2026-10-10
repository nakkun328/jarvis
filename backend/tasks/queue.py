"""A small single-worker task queue over `TaskRepository`.

The queue is FIFO over pending tasks and claims one atomically, so a task is
never started twice. It runs one executor at a time under a timeout and a
cancellation token. The executor's own claim of success is never enough: a
separate `Verifier` checks the postcondition, and only a verified result
completes a task. There is no daemon, scheduler, automatic retry, or multi-worker
coordination here; callers decide when to call `run_next`.

Executors run on the caller's event loop. Cancellation and timeouts rely on
the executor yielding to the loop or polling the token; blocking calls inside an
executor cannot be interrupted.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import UUID

from backend.tasks.models import (
    DEFAULT_MAX_ATTEMPTS,
    StepStatus,
    Task,
    TaskFailure,
    TaskStatus,
    VerificationState,
    WaitingReason,
)
from backend.tasks.repository import (
    InvalidTransition,
    TaskNotFound,
    TaskRepository,
    TaskRepositoryError,
    TaskStateChanged,
)
from backend.tasks.validation import bounded_text

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.02
DEFAULT_CANCEL_GRACE_SECONDS = 1.0
MAX_OUTCOME_SUMMARY_CHARS = 4000


class TaskNotCancellable(RuntimeError):
    """A running task can be cancelled only by the queue that is running it."""


class CancellationToken:
    """Cooperative cancellation signal; safe to set from any thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    @property
    def is_set(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ExecutionOutcome:
    """What an executor reports. Exactly one of the three fields is meaningful.

    - `summary`: the executor claims it finished; this must still be verified.
    - `waiting`: the executor cannot continue until someone acts.
    - `failed`: the executor gave up without a success claim.
    """

    summary: str | None = None
    waiting: WaitingReason | None = None
    failed: bool = False

    def __post_init__(self) -> None:
        chosen = [self.summary is not None, self.waiting is not None, self.failed is True]
        if sum(chosen) != 1:
            raise ValueError("an outcome is exactly one of summary, waiting, or failed")
        if self.summary is not None:
            bounded_text(self.summary, "summary", MAX_OUTCOME_SUMMARY_CHARS)
        if self.waiting is not None and not isinstance(self.waiting, WaitingReason):
            raise ValueError("waiting must be a WaitingReason")

    @classmethod
    def succeeded(cls, summary: str) -> "ExecutionOutcome":
        return cls(summary=summary)

    @classmethod
    def wait(cls, reason: WaitingReason) -> "ExecutionOutcome":
        return cls(waiting=reason)

    @classmethod
    def failure(cls) -> "ExecutionOutcome":
        return cls(failed=True)


@dataclass(frozen=True)
class VerificationResult:
    """Whether the task's postcondition holds in the observed state."""

    passed: bool


class ProgressReporter:
    """Lets an executor record step progress for the task it is running."""

    def __init__(self, repository: TaskRepository, task_id: UUID) -> None:
        self._repository = repository
        self._task_id = task_id

    def start_step(self, index: int) -> None:
        self._repository.start_step(self._task_id, index)

    def finish_step(
        self,
        index: int,
        *,
        status: StepStatus = StepStatus.COMPLETED,
        note: str | None = None,
    ) -> None:
        self._repository.finish_step(self._task_id, index, status=status, note=note)


class TaskExecutor(Protocol):
    async def execute(
        self, task: Task, progress: ProgressReporter, cancellation: CancellationToken
    ) -> ExecutionOutcome:
        """Do the work. `task` is an immutable snapshot; its goal is data, not instructions."""
        ...


class Verifier(Protocol):
    async def verify(self, task: Task, outcome: ExecutionOutcome) -> VerificationResult:
        """Check the postcondition against real state, independently of the executor."""
        ...


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


class TaskQueue:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0 or cancel_grace_seconds < 0:
            raise ValueError("timeouts must be positive")
        self.repository = repository
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self._poll = poll_interval_seconds
        self._grace = cancel_grace_seconds
        self._tokens: dict[UUID, CancellationToken] = {}
        self._tokens_lock = threading.Lock()

    # ----- submit / cancel / retry -----

    def submit(
        self,
        goal: str,
        steps: Sequence[str],
        *,
        target_device: str | None = None,
    ) -> Task:
        """Queue a task; it stays pending until a worker claims it."""
        return self.repository.create_task(goal, steps, target_device=target_device)

    def cancel(self, task_id: UUID) -> Task:
        """Cancel a pending or waiting task now, or signal a task this queue is running.

        For a running task the returned snapshot is still `running`; it becomes
        `cancelled` when the in-progress `run_next`/`resume` observes the token.
        """
        for _ in range(3):
            task = self.repository.get_task(task_id)
            if task is None:
                raise TaskNotFound("Task does not exist")
            if task.status in (TaskStatus.PENDING, TaskStatus.WAITING):
                try:
                    return self.repository.transition(task.id, task.status, TaskStatus.CANCELLED)
                except TaskStateChanged:
                    continue
            if task.status is TaskStatus.RUNNING:
                with self._tokens_lock:
                    token = self._tokens.get(task.id)
                if token is None:
                    raise TaskNotCancellable("Task is not running in this queue")
                token.set()
                return task
            raise InvalidTransition(f"Cannot cancel a {task.status.value} task")
        raise TaskStateChanged("Task status kept changing")

    def retry(self, task_id: UUID) -> Task:
        """Explicitly start a new attempt of a failed or cancelled task."""
        return self.repository.retry(task_id, max_attempts=self.max_attempts)

    # ----- running -----

    async def run_next(
        self,
        executor: TaskExecutor,
        verifier: Verifier,
        *,
        timeout_seconds: float | None = None,
    ) -> Task | None:
        """Claim and run the oldest pending task; None when nothing is pending."""
        task = self.repository.claim_next()
        if task is None:
            return None
        return await self._run_claimed(task, executor, verifier, timeout_seconds)

    async def resume(
        self,
        task_id: UUID,
        executor: TaskExecutor,
        verifier: Verifier,
        *,
        timeout_seconds: float | None = None,
    ) -> Task:
        """Explicitly continue a waiting task. Nothing resumes a waiting task on its own."""
        self.repository.transition(task_id, TaskStatus.WAITING, TaskStatus.RUNNING)
        task = self.repository.get_task(task_id)
        if task is None:
            raise TaskNotFound("Task does not exist")
        return await self._run_claimed(task, executor, verifier, timeout_seconds)

    async def _run_claimed(
        self,
        task: Task,
        executor: TaskExecutor,
        verifier: Verifier,
        timeout_seconds: float | None,
    ) -> Task:
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        token = CancellationToken()
        with self._tokens_lock:
            self._tokens[task.id] = token
        try:
            return await self._drive(task, executor, verifier, token, timeout)
        except asyncio.CancelledError:
            # The worker itself was cancelled mid-task. The task may already have
            # had side effects, so it is recorded as interrupted, never re-queued.
            with suppress(TaskRepositoryError):
                self._settle(
                    task.id,
                    lambda: self.repository.transition(
                        task.id,
                        TaskStatus.RUNNING,
                        TaskStatus.FAILED,
                        failure_code=TaskFailure.INTERRUPTED,
                    ),
                )
            raise
        finally:
            with self._tokens_lock:
                self._tokens.pop(task.id, None)

    async def _drive(
        self,
        task: Task,
        executor: TaskExecutor,
        verifier: Verifier,
        token: CancellationToken,
        timeout: float,
    ) -> Task:
        repository = self.repository
        deadline = asyncio.get_running_loop().time() + timeout
        progress = ProgressReporter(repository, task.id)
        try:
            outcome = await self._guarded(executor.execute(task, progress, token), token, deadline)
            if not isinstance(outcome, ExecutionOutcome):
                raise TypeError("executor must return an ExecutionOutcome")
            if outcome.waiting is not None:
                return self._settle(
                    task.id,
                    lambda: repository.transition(
                        task.id,
                        TaskStatus.RUNNING,
                        TaskStatus.WAITING,
                        waiting_reason=outcome.waiting,
                    ),
                )
            if outcome.failed or outcome.summary is None:
                return self._fail(task.id, TaskFailure.EXECUTION_FAILED)
            snapshot = repository.get_task(task.id) or task
            result = await self._guarded(verifier.verify(snapshot, outcome), token, deadline)
            if not isinstance(result, VerificationResult):
                raise TypeError("verifier must return a VerificationResult")
            if not result.passed:
                return self._settle(task.id, lambda: repository.fail_verification(task.id))
            summary = outcome.summary
            return self._settle(
                task.id,
                lambda: repository.complete(task.id, summary, VerificationState.VERIFIED),
            )
        except _Cancelled:
            return self._settle(
                task.id,
                lambda: repository.transition(task.id, TaskStatus.RUNNING, TaskStatus.CANCELLED),
            )
        except _TimedOut:
            return self._fail(task.id, TaskFailure.TIMEOUT)
        except Exception as exc:
            # Only the exception type is logged; messages may carry untrusted or
            # sensitive text and are never stored or returned.
            logger.error("task %s failed internally: %s", task.id, type(exc).__name__)
            return self._fail(task.id, TaskFailure.INTERNAL_ERROR)

    def _fail(self, task_id: UUID, code: TaskFailure) -> Task:
        return self._settle(
            task_id,
            lambda: self.repository.transition(
                task_id, TaskStatus.RUNNING, TaskStatus.FAILED, failure_code=code
            ),
        )

    def _settle(self, task_id: UUID, action: Callable[[], Task]) -> Task:
        """Apply a final transition; if someone else already moved the task, report that state."""
        try:
            return action()
        except TaskStateChanged:
            task = self.repository.get_task(task_id)
            if task is None:
                raise TaskNotFound("Task does not exist") from None
            return task
        except TaskRepositoryError:
            logger.error("task %s could not record its final state", task_id)
            raise

    async def _guarded(
        self, awaitable: Awaitable[_T], token: CancellationToken, deadline: float
    ) -> _T:
        """Await executor or verifier work, enforcing cancellation and the deadline."""
        loop = asyncio.get_running_loop()
        future = asyncio.ensure_future(awaitable)
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0 and not future.done():
                    token.set()
                    await self._abort(future)
                    raise _TimedOut
                done, _ = await asyncio.wait({future}, timeout=max(min(self._poll, remaining), 0))
                if token.is_set:
                    await self._abort(future)
                    raise _Cancelled
                if done:
                    if future.cancelled():
                        raise RuntimeError("work was cancelled unexpectedly")
                    return future.result()
        finally:
            if not future.done():
                future.cancel()

    async def _abort(self, future: asyncio.Future[object]) -> None:
        if not future.done():
            future.cancel()
            await asyncio.wait({future}, timeout=self._grace)
        if future.done() and not future.cancelled():
            future.exception()  # mark retrieved; its content is discarded
