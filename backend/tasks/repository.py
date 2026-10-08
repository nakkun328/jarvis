"""Durable tasks with compare-and-swap state transitions.

The repository stores what callers hand it and what the state machine allows.
It never runs anything. Failure and waiting causes use fixed codes, and a task
can only be completed through `complete`, which requires a verified outcome:
an executor claiming success is not a postcondition.

There is deliberately no physical deletion API, and finished tasks never change.
Re-running a failed or cancelled task is an explicit `retry` that creates a new
task row (a new attempt) linked to the original.
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from backend.core.database import Database
from backend.tasks.models import (
    ALLOWED_TRANSITIONS,
    DEFAULT_MAX_ATTEMPTS,
    FINISHING_STEP_STATUSES,
    MAX_ATTEMPTS_LIMIT,
    MAX_GOAL_CHARS,
    MAX_RESULT_SUMMARY_CHARS,
    MAX_STEP_DESCRIPTION_CHARS,
    MAX_STEP_NOTE_CHARS,
    MAX_STEPS,
    MAX_TARGET_DEVICE_CHARS,
    TERMINAL_STATUSES,
    StepStatus,
    Task,
    TaskFailure,
    TaskStatus,
    TaskStep,
    VerificationState,
    WaitingReason,
)
from backend.tasks.validation import bounded_text, optional_text

logger = logging.getLogger(__name__)


class TaskRepositoryError(RuntimeError):
    """Task data could not be stored or read."""


class TaskNotFound(TaskRepositoryError):
    """The task does not exist."""


class TaskStateChanged(TaskRepositoryError):
    """The task is terminal or its status changed since the caller observed it."""


class InvalidTransition(TaskRepositoryError):
    """The requested status change is not in the allowed-transition table."""


class VerificationRequired(TaskRepositoryError):
    """Completion needs a verified postcondition."""


class ProgressRejected(TaskRepositoryError):
    """A step update would move progress backwards or break step order."""


class RetryNotAllowed(TaskRepositoryError):
    """The task cannot start another attempt."""


class TaskIntegrityError(TaskRepositoryError):
    """A record violates storage constraints."""


class TaskRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    # ----- creation and reads -----

    def create_task(
        self,
        goal: str,
        steps: Sequence[str],
        *,
        target_device: str | None = None,
    ) -> Task:
        """Create a pending task with its steps fixed up front."""
        goal = bounded_text(goal, "goal", MAX_GOAL_CHARS)
        descriptions = _step_descriptions(steps)
        target_device = optional_text(target_device, "target_device", MAX_TARGET_DEVICE_CHARS)
        task_id = uuid4()
        with self._write() as connection:
            self._insert(
                connection,
                task_id,
                goal,
                descriptions,
                target_device,
                attempt=1,
                retry_of=None,
                now=self._now(),
            )
            return _load(connection, task_id)

    def get_task(self, task_id: UUID) -> Task | None:
        _require_uuid(task_id, "task_id")
        with self._read() as connection:
            try:
                return _load(connection, task_id)
            except TaskNotFound:
                return None

    def list_tasks(self, status: TaskStatus | None = None, *, limit: int = 100) -> list[Task]:
        if status is not None and not isinstance(status, TaskStatus):
            raise ValueError("status must be a TaskStatus")
        _require_limit(limit)
        query = "SELECT id FROM tasks"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY created_at, rowid LIMIT ?"
        params.append(limit)
        with self._read() as connection:
            ids = [UUID(row["id"]) for row in connection.execute(query, params)]
            return [_load(connection, task_id) for task_id in ids]

    # ----- state transitions -----

    def transition(
        self,
        task_id: UUID,
        expected: TaskStatus,
        new: TaskStatus,
        *,
        failure_code: TaskFailure | None = None,
        waiting_reason: WaitingReason | None = None,
    ) -> Task:
        """Compare-and-swap the status.

        Completion goes through `complete`, and a verification failure through
        `fail_verification`, so neither can be recorded without its evidence.
        """
        _require_uuid(task_id, "task_id")
        if not isinstance(expected, TaskStatus) or not isinstance(new, TaskStatus):
            raise ValueError("expected and new must be TaskStatus values")
        if new not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransition(f"Cannot move task from {expected.value} to {new.value}")
        if new is TaskStatus.COMPLETED:
            raise InvalidTransition("Completion requires verification; use complete")
        if new is TaskStatus.FAILED:
            if not isinstance(failure_code, TaskFailure):
                raise ValueError("Failure requires a TaskFailure code")
            if failure_code is TaskFailure.VERIFICATION_FAILED:
                raise InvalidTransition("Use fail_verification to record a verification failure")
        elif failure_code is not None:
            raise ValueError("Only a failed task accepts a failure code")
        if new is TaskStatus.WAITING:
            if not isinstance(waiting_reason, WaitingReason):
                raise ValueError("Waiting requires a WaitingReason")
        elif waiting_reason is not None:
            raise ValueError("Only a waiting task accepts a waiting reason")
        with self._write() as connection:
            self._swap(
                connection,
                task_id,
                expected,
                new,
                failure_code=failure_code,
                waiting_reason=waiting_reason,
            )
            return _load(connection, task_id)

    def complete(
        self,
        task_id: UUID,
        result_summary: str,
        verification: VerificationState,
    ) -> Task:
        """Complete a running task, only with a verified postcondition."""
        _require_uuid(task_id, "task_id")
        result_summary = bounded_text(result_summary, "result_summary", MAX_RESULT_SUMMARY_CHARS)
        if not isinstance(verification, VerificationState):
            raise ValueError("verification must be a VerificationState")
        if verification is not VerificationState.VERIFIED:
            raise VerificationRequired("A task is completed only when its result is verified")
        with self._write() as connection:
            self._swap(
                connection,
                task_id,
                TaskStatus.RUNNING,
                TaskStatus.COMPLETED,
                result_summary=result_summary,
                verified=VerificationState.VERIFIED,
            )
            return _load(connection, task_id)

    def fail_verification(self, task_id: UUID) -> Task:
        """Fail a running task whose postcondition did not hold."""
        _require_uuid(task_id, "task_id")
        with self._write() as connection:
            self._swap(
                connection,
                task_id,
                TaskStatus.RUNNING,
                TaskStatus.FAILED,
                failure_code=TaskFailure.VERIFICATION_FAILED,
                verified=VerificationState.VERIFICATION_FAILED,
            )
            return _load(connection, task_id)

    def claim_next(self) -> Task | None:
        """Atomically move the oldest pending task to running.

        The select and the guarded update share one immediate transaction, so
        two callers on separate connections can never claim the same task.
        """
        with self._write() as connection:
            row = connection.execute(
                "SELECT id FROM tasks WHERE status = ? ORDER BY created_at, rowid LIMIT 1",
                (TaskStatus.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            task_id = UUID(row["id"])
            now = _ts(self._now())
            updated = connection.execute(
                "UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?), "
                "updated_at = ? WHERE id = ? AND status = ?",
                (
                    TaskStatus.RUNNING.value,
                    now,
                    now,
                    str(task_id),
                    TaskStatus.PENDING.value,
                ),
            )
            if updated.rowcount != 1:
                return None
            return _load(connection, task_id)

    # ----- step progress -----

    def start_step(self, task_id: UUID, index: int) -> Task:
        """Make `index` the current step. Progress only moves forward."""
        _require_uuid(task_id, "task_id")
        _require_index(index)
        with self._write() as connection:
            task_row = self._require_running(connection, task_id)
            step = connection.execute(
                "SELECT status FROM task_steps WHERE task_id = ? AND step_index = ?",
                (str(task_id), index),
            ).fetchone()
            if step is None:
                raise ProgressRejected("Task has no such step")
            current = task_row["current_step"]
            if current is not None:
                if index <= current:
                    raise ProgressRejected("Step progress cannot move backwards")
                running = connection.execute(
                    "SELECT status FROM task_steps WHERE task_id = ? AND step_index = ?",
                    (str(task_id), current),
                ).fetchone()
                if running["status"] == StepStatus.RUNNING.value:
                    raise ProgressRejected("Finish the current step before starting another")
            now = _ts(self._now())
            connection.execute(
                "UPDATE task_steps SET status = ?, started_at = ? "
                "WHERE task_id = ? AND step_index = ?",
                (StepStatus.RUNNING.value, now, str(task_id), index),
            )
            connection.execute(
                "UPDATE tasks SET current_step = ?, updated_at = ? WHERE id = ?",
                (index, now, str(task_id)),
            )
            return _load(connection, task_id)

    def finish_step(
        self,
        task_id: UUID,
        index: int,
        *,
        status: StepStatus = StepStatus.COMPLETED,
        note: str | None = None,
    ) -> Task:
        """Finish the current running step and optionally attach a bounded note."""
        _require_uuid(task_id, "task_id")
        _require_index(index)
        if status not in FINISHING_STEP_STATUSES:
            raise ValueError("A step can finish as completed, failed, or skipped")
        note = optional_text(note, "note", MAX_STEP_NOTE_CHARS)
        with self._write() as connection:
            task_row = self._require_running(connection, task_id)
            if task_row["current_step"] != index:
                raise ProgressRejected("Only the current step can be finished")
            now = _ts(self._now())
            updated = connection.execute(
                "UPDATE task_steps SET status = ?, finished_at = ?, note = ? "
                "WHERE task_id = ? AND step_index = ? AND status = ?",
                (
                    status.value,
                    now,
                    note,
                    str(task_id),
                    index,
                    StepStatus.RUNNING.value,
                ),
            )
            if updated.rowcount != 1:
                raise ProgressRejected("Step is not running")
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, str(task_id)))
            return _load(connection, task_id)

    # ----- restart recovery and retry -----

    def recover_in_flight(self) -> list[Task]:
        """Fail every task left `running` by a previous process as `interrupted`.

        Policy: a task that was running when the process stopped may already
        have had external side effects, which are not known to be idempotent,
        so it is never silently re-run. It becomes failed(interrupted) and a
        person may start a new attempt with `retry`. Pending tasks stay pending
        and waiting tasks stay waiting. Call this once at startup, before any
        worker is started, since this is a single-worker design.
        """
        with self._write() as connection:
            ids = [
                UUID(row["id"])
                for row in connection.execute(
                    "SELECT id FROM tasks WHERE status = ? ORDER BY created_at, rowid",
                    (TaskStatus.RUNNING.value,),
                )
            ]
            for task_id in ids:
                self._swap(
                    connection,
                    task_id,
                    TaskStatus.RUNNING,
                    TaskStatus.FAILED,
                    failure_code=TaskFailure.INTERRUPTED,
                )
            recovered = [_load(connection, task_id) for task_id in ids]
        if recovered:
            logger.warning("marked %d in-flight task(s) as interrupted", len(recovered))
        return recovered

    def retry(self, task_id: UUID, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> Task:
        """Start a new attempt of a failed or cancelled task.

        The original stays as it was. The new pending task copies the goal,
        step descriptions, and device label, with `attempt` one higher and
        `retry_of` pointing back. There is never an automatic retry, a task can
        be retried at most once, and attempts are bounded by `max_attempts`.
        """
        _require_uuid(task_id, "task_id")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT
        ):
            raise ValueError(f"max_attempts must be an integer from 1 to {MAX_ATTEMPTS_LIMIT}")
        with self._write() as connection:
            original = _load(connection, task_id)
            if original.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                raise RetryNotAllowed("Only a failed or cancelled task can be retried")
            if original.attempt >= max_attempts:
                raise RetryNotAllowed("Task has reached its attempt limit")
            already = connection.execute(
                "SELECT 1 FROM tasks WHERE retry_of = ?", (str(task_id),)
            ).fetchone()
            if already is not None:
                raise RetryNotAllowed("Task has already been retried")
            new_id = uuid4()
            self._insert(
                connection,
                new_id,
                original.goal,
                [step.description for step in original.steps],
                original.target_device,
                attempt=original.attempt + 1,
                retry_of=original.id,
                now=self._now(),
            )
            return _load(connection, new_id)

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
            raise TaskIntegrityError("Task record violates storage constraints") from exc
        except (OSError, sqlite3.Error) as exc:
            raise TaskRepositoryError("Task storage unavailable") from exc

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        task_id: UUID,
        goal: str,
        descriptions: Sequence[str],
        target_device: str | None,
        *,
        attempt: int,
        retry_of: UUID | None,
        now: datetime,
    ) -> None:
        stamp = _ts(now)
        connection.execute(
            "INSERT INTO tasks (id, goal, target_device, status, created_at, updated_at, "
            "attempt, retry_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(task_id),
                goal,
                target_device,
                TaskStatus.PENDING.value,
                stamp,
                stamp,
                attempt,
                str(retry_of) if retry_of is not None else None,
            ),
        )
        connection.executemany(
            "INSERT INTO task_steps (task_id, step_index, description, status) VALUES (?, ?, ?, ?)",
            [
                (str(task_id), index, description, StepStatus.PENDING.value)
                for index, description in enumerate(descriptions)
            ],
        )

    def _swap(
        self,
        connection: sqlite3.Connection,
        task_id: UUID,
        expected: TaskStatus,
        new: TaskStatus,
        *,
        failure_code: TaskFailure | None = None,
        waiting_reason: WaitingReason | None = None,
        result_summary: str | None = None,
        verified: VerificationState | None = None,
    ) -> None:
        """One guarded UPDATE; the caller has already validated the transition."""
        now = _ts(self._now())
        updated = connection.execute(
            "UPDATE tasks SET status = ?, failure_code = ?, waiting_reason = ?, "
            "result_summary = ?, verified = COALESCE(?, verified), "
            "started_at = COALESCE(started_at, ?), finished_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (
                new.value,
                failure_code.value if failure_code else None,
                waiting_reason.value if waiting_reason else None,
                result_summary,
                verified.value if verified else None,
                now if new is TaskStatus.RUNNING else None,
                now if new in TERMINAL_STATUSES else None,
                now,
                str(task_id),
                expected.value,
            ),
        )
        if updated.rowcount != 1:
            known = connection.execute("SELECT 1 FROM tasks WHERE id = ?", (str(task_id),))
            if known.fetchone() is None:
                raise TaskNotFound("Task does not exist")
            raise TaskStateChanged("Task status changed")
        leftover = {
            TaskStatus.FAILED: StepStatus.FAILED,
            TaskStatus.CANCELLED: StepStatus.CANCELLED,
        }.get(new)
        if leftover is not None:
            connection.execute(
                "UPDATE task_steps SET status = ?, finished_at = ? "
                "WHERE task_id = ? AND status = ?",
                (leftover.value, now, str(task_id), StepStatus.RUNNING.value),
            )

    @staticmethod
    def _require_running(connection: sqlite3.Connection, task_id: UUID) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if row is None:
            raise TaskNotFound("Task does not exist")
        if row["status"] != TaskStatus.RUNNING.value:
            raise TaskStateChanged("Task is not running")
        return row


def _ts(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TaskRepositoryError("Stored task timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise TaskRepositoryError("Stored task timestamp is invalid")
    return parsed.astimezone(UTC)


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def _require_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")


def _require_index(index: object) -> None:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("step index must be a non-negative integer")


def _step_descriptions(steps: object) -> list[str]:
    if isinstance(steps, str | bytes) or not isinstance(steps, Sequence):
        raise ValueError("steps must be a sequence of step descriptions")
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"a task needs 1 to {MAX_STEPS} steps")
    return [
        bounded_text(step, f"step {index}", MAX_STEP_DESCRIPTION_CHARS)
        for index, step in enumerate(steps)
    ]


def _load(connection: sqlite3.Connection, task_id: UUID) -> Task:
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
    if row is None:
        raise TaskNotFound("Task does not exist")
    step_rows = connection.execute(
        "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index", (str(task_id),)
    ).fetchall()
    try:
        return Task(
            id=UUID(row["id"]),
            goal=row["goal"],
            steps=tuple(
                TaskStep(
                    index=step["step_index"],
                    description=step["description"],
                    status=StepStatus(step["status"]),
                    started_at=_parse_ts(step["started_at"]),
                    finished_at=_parse_ts(step["finished_at"]),
                    note=step["note"],
                )
                for step in step_rows
            ),
            current_step=row["current_step"],
            target_device=row["target_device"],
            status=TaskStatus(row["status"]),
            created_at=_parse_ts(row["created_at"]),
            updated_at=_parse_ts(row["updated_at"]),
            started_at=_parse_ts(row["started_at"]),
            finished_at=_parse_ts(row["finished_at"]),
            result_summary=row["result_summary"],
            failure_code=TaskFailure(row["failure_code"]) if row["failure_code"] else None,
            waiting_reason=WaitingReason(row["waiting_reason"]) if row["waiting_reason"] else None,
            attempt=row["attempt"],
            verified=VerificationState(row["verified"]),
            retry_of=UUID(row["retry_of"]) if row["retry_of"] else None,
        )
    except ValueError as exc:
        raise TaskRepositoryError("Stored task is invalid") from exc
