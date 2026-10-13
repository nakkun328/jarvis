"""A group discussion as a background task (docs/discussion.md), mirroring the chat tools.

Starting a discussion creates a conversation that holds the user's topic, queues a task whose goal
is a fixed label plus the conversation id and a job id (never user text), and returns at once. One
worker runs the :class:`DiscussionEngine` and posts every turn into the conversation as an
assistant message the moment it exists, in the prefixed format of ``format.py``. The summary comes
last. Cancelling the task stops the discussion before the next turn; a cancelled discussion with at
least one turn gets a short moderator summary of what was said, then a notice.

Limits: rounds are clamped to ``JARVIS_DISCUSSION_MAX_ROUNDS``, the number of discussions per day
is counted from the task table (``JARVIS_DISCUSSION_PER_DAY_LIMIT``; no table of its own), models
must be entries of the server allowlist, and only one discussion runs at a time. Nothing here
logs or echoes the topic, the persona text, a turn or a provider message: fixed codes only.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from backend.chat.context import (
    ConversationCapacityError,
    ConversationNotFound,
    ConversationStore,
    clean_posted_text,
)
from backend.chat.persistence import ConversationStorageError
from backend.discussion.engine import (
    DEFAULT_ROUNDS,
    MAX_ROUNDS,
    MODERATOR_SYSTEM,
    SUMMARY_INSTRUCTION,
    SUMMARY_MAX_CHARS,
    TOPIC_MAX_CHARS,
    DiscussionEngine,
    DiscussionMessage,
    MessageKind,
    StopCode,
)
from backend.discussion.errors import DiscussionError, ErrorCode
from backend.discussion.format import (
    RESERVED_SPEAKERS,
    format_message,
    format_notice,
    format_summary,
)
from backend.discussion.personas import (
    NAME_MAX,
    PALETTE,
    PERSONAS_MAX,
    PERSONAS_MIN,
    Persona,
    parse_personas,
    persona_from_dict,
    preset_personas,
)
from backend.discussion.store import PersonaStore
from backend.providers.base import ChatMessage, CompletionRequest, LLMProvider
from backend.providers.choices import ModelChoiceError, ModelRegistry
from backend.tasks.models import (
    DISCUSSION_GOAL_PREFIX,
    StepStatus,
    Task,
    TaskFailure,
    TaskStatus,
    discussion_goal,
    parse_discussion_goal,
)
from backend.tasks.queue import (
    CancellationToken,
    ExecutionOutcome,
    ProgressReporter,
    TaskQueue,
    VerificationResult,
)
from backend.tasks.repository import TaskRepositoryError, TaskStateChanged

_LOG = logging.getLogger(__name__)

DEFAULT_WORKER_POLL_SECONDS = 1.0
DEFAULT_CALL_TIMEOUT_SECONDS = 90.0
POSTED_SUMMARY = "議論を会話に投稿しました。"
CANCELLED_NOTICE = ("議論は停止されました。", "CANCELLED")
_FAILURE_NOTICE = {
    TaskFailure.EXECUTION_FAILED: ("議論を完了できませんでした。", "EXECUTION_FAILED"),
    TaskFailure.VERIFICATION_FAILED: (
        "結果を会話で確認できなかったため、議論を失敗にしました。",
        "VERIFICATION_FAILED",
    ),
    TaskFailure.TIMEOUT: ("議論が制限時間内に終わらなかったため、中断しました。", "TIMEOUT"),
    TaskFailure.INTERRUPTED: (
        "サーバーの再起動などで、議論が中断されました。",
        "INTERRUPTED",
    ),
    TaskFailure.INTERNAL_ERROR: (
        "内部エラーのため、議論を完了できませんでした。",
        "INTERNAL_ERROR",
    ),
}
_ACTIVE = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING)
_PARTIAL_INSTRUCTION = (
    "この議論は途中で停止されました。ここまでの発言だけで、短くまとめてください。"
)
_PARTIAL_TRANSCRIPT_CHARS = 4000


class NotADiscussionTask(Exception):
    """The task is not a discussion task."""


@dataclass
class _Job:
    topic: str
    personas: tuple[Persona, ...]
    rounds: int
    conversation_id: UUID
    steps_total: int
    engine: DiscussionEngine | None = None
    task_id: UUID | None = None
    token: CancellationToken | None = None
    progress: ProgressReporter | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)
    last_posted: str | None = None
    summary_posted: bool = False
    summary_text: str | None = None  # the moderator's summary, kept for the memory hook
    noticed: bool = False  # a notice for the way it ended is already in the conversation
    finished: bool = False  # the closing posts (cancel summary, failure notice) were made
    next_step: int = 0
    open_step: int | None = None


class DiscussionService:
    def __init__(
        self,
        queue: TaskQueue,
        store: ConversationStore,
        models: ModelRegistry,
        moderator: LLMProvider,
        *,
        model_choices: tuple[str, ...],
        max_rounds: int,
        per_day_limit: int,
        clock: Callable[[], datetime] | None = None,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        worker_poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS,
        persona_store: PersonaStore | None = None,
        on_summary: Callable[[UUID, str, str], None] | None = None,
    ) -> None:
        """``on_summary(conversation_id, topic, summary)`` is the optional memory hook
        (docs/memory.md). It runs in a worker thread once per finished discussion that has at
        least one turn and a summary; any failure is logged as a fixed code and ignored."""
        self.queue = queue
        self._persona_store = persona_store or PersonaStore(None)
        self._on_summary = on_summary
        self._store = store
        self._models = models
        self._moderator = moderator
        self._choices = tuple(model_choices)
        self._max_rounds = min(max_rounds, MAX_ROUNDS)
        self._per_day = per_day_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._call_timeout = call_timeout_seconds
        self._poll = worker_poll_seconds
        self._jobs: dict[str, _Job] = {}
        self._start_lock = asyncio.Lock()
        self._loop_ref: asyncio.AbstractEventLoop | None = None
        # Every call a discussion may make, plus room for the summary after a cancel.
        self.task_timeout_seconds = (PERSONAS_MAX * MAX_ROUNDS + 2) * call_timeout_seconds + 120.0

    # ----- configuration (read by the screen) -----

    def _day_start(self) -> datetime:
        local = self._clock().astimezone()
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _used_today(self) -> int:
        return self.queue.repository.count_goal_tasks(
            DISCUSSION_GOAL_PREFIX, since=self._day_start()
        )

    def _presets(self) -> tuple[Persona, ...]:
        options = self._models.options()
        ready = [option.id for option in options if option.available]
        return preset_personas(ready or list(self._choices))

    # ----- custom personas (docs/discussion.md) -----

    def _custom_dict(self, persona: Persona, available: Mapping[str, bool]) -> dict[str, Any]:
        return {**persona.as_dict(), "available": available.get(persona.model, False)}

    def personas(self) -> dict[str, Any]:
        """Presets and the saved custom personas. A saved persona whose model is no longer usable
        is listed with ``available`` false, not removed. A store that cannot be read gives no
        custom personas and a ``store`` state the screen can show."""
        available = {option.id: option.available for option in self._models.options()}
        store = self._persona_store
        state = store.state()
        custom = [self._custom_dict(p, available) for p in store.list()] if state == "ok" else []
        return {
            "presets": [persona.as_dict() for persona in self._presets()],
            "custom": custom,
            "max_custom": store.max_personas,
            "store": state,
        }

    def _one_persona(self, raw: object, index: int) -> Persona:
        return persona_from_dict(raw, index, self._choices)

    def create_persona(self, raw: object) -> dict[str, Any]:
        store = self._persona_store
        persona = self._one_persona(raw, len(store.list()))
        saved = store.create(persona, self._choices)
        return self._custom_dict(saved, self._available())

    @staticmethod
    def _check_name(name: object) -> None:
        if not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX or not name.isprintable():
            raise DiscussionError(ErrorCode.NAME_INVALID)

    def replace_persona(self, name: str, raw: object) -> dict[str, Any]:
        self._check_name(name)
        store = self._persona_store
        persona = self._one_persona(raw, 0)
        saved = store.replace(name, persona, self._choices)
        return self._custom_dict(saved, self._available())

    def delete_persona(self, name: str) -> None:
        self._check_name(name)
        self._persona_store.delete(name)

    def _available(self) -> dict[str, bool]:
        return {option.id: option.available for option in self._models.options()}

    def config(self) -> dict[str, Any]:
        repository = self.queue.repository
        listing = self.personas()
        used = self._used_today()
        active = None
        for status in (TaskStatus.RUNNING, TaskStatus.PENDING):
            found = repository.list_goal_tasks(DISCUSSION_GOAL_PREFIX, status)
            if found:
                parsed = parse_discussion_goal(found[0].goal)
                if parsed is not None:
                    active = {"task_id": str(found[0].id), "conversation_id": str(parsed[0])}
                    break
        return {
            "enabled": True,
            "max_rounds": self._max_rounds,
            "default_rounds": min(DEFAULT_ROUNDS, self._max_rounds),
            "per_day_limit": self._per_day,
            "used_today": used,
            "remaining_today": max(self._per_day - used, 0),
            "participants_min": PERSONAS_MIN,
            "participants_max": PERSONAS_MAX,
            "topic_max_chars": TOPIC_MAX_CHARS,
            "models": [option.as_dict() for option in self._models.options()],
            "presets": [persona.as_dict() for persona in self._presets()],
            "custom_personas": listing["custom"],
            "personas_store": listing["store"],
            "max_custom_personas": listing["max_custom"],
            "palette": list(PALETTE),
            "active": active,
        }

    # ----- start -----

    def _personas(self, raw: object) -> tuple[Persona, ...]:
        if not isinstance(raw, list):
            raise DiscussionError(ErrorCode.REQUEST_INVALID)
        presets = {persona.name: persona for persona in self._presets()}
        items: list[object] = []
        for item in raw:
            if isinstance(item, str):
                preset = presets.get(item)
                if preset is None:
                    preset = self._saved_persona(item)
                items.append(preset.as_dict())
            elif isinstance(item, Mapping):
                items.append(item)
            else:
                raise DiscussionError(ErrorCode.PERSONA_INVALID)
        personas = parse_personas(items, self._choices)
        if any(persona.name in RESERVED_SPEAKERS for persona in personas):
            raise DiscussionError(ErrorCode.NAME_INVALID)
        for model in dict.fromkeys(persona.model for persona in personas):
            try:
                self._models.provider_for(model)
            except ModelChoiceError:
                raise DiscussionError(ErrorCode.MODEL_UNAVAILABLE) from None
        return personas

    def _saved_persona(self, name: str) -> Persona:
        """A saved custom persona by name, read from the store now (the job then keeps this
        snapshot). No store, or no such name: an unknown preset."""
        store = self._persona_store
        try:
            found = store.get(name)
        except DiscussionError as error:
            if error.code is ErrorCode.STORE_CORRUPT:
                raise
            found = None
        if found is None:
            raise DiscussionError(ErrorCode.PRESET_UNKNOWN)
        return found

    def _rounds(self, raw: object) -> int:
        if raw is None:
            return min(DEFAULT_ROUNDS, self._max_rounds)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise DiscussionError(ErrorCode.ROUNDS_INVALID)
        return min(raw, self._max_rounds)

    async def start(
        self, topic: object, personas: object, rounds: object = None
    ) -> tuple[UUID, UUID]:
        """Queue a discussion. Returns (conversation id, task id). Raises ``DiscussionError``
        with a fixed code; the message never carries the offered text."""
        if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= TOPIC_MAX_CHARS:
            raise DiscussionError(ErrorCode.TOPIC_INVALID)
        chosen = self._personas(personas)
        round_count = self._rounds(rounds)
        async with self._start_lock:
            repository = self.queue.repository
            if repository.count_goal_tasks(DISCUSSION_GOAL_PREFIX, statuses=_ACTIVE) > 0:
                raise DiscussionError(ErrorCode.BUSY)
            if self._used_today() >= self._per_day:
                raise DiscussionError(ErrorCode.DAILY_LIMIT)
            try:
                conversation_id = await self._store.start_conversation(topic.strip())
            except (ConversationStorageError, ConversationCapacityError):
                raise DiscussionError(ErrorCode.STORAGE_UNAVAILABLE) from None
            job_id = uuid4().hex
            steps = [f"発言 {i + 1}" for i in range(len(chosen) * round_count)] + ["まとめ"]
            job = _Job(topic.strip(), chosen, round_count, conversation_id, len(steps))
            job.engine = self._engine(job)
            self._jobs[job_id] = job
            try:
                task = self.queue.submit(discussion_goal(conversation_id, job_id), steps)
            except Exception:
                self._jobs.pop(job_id, None)
                raise DiscussionError(ErrorCode.STORAGE_UNAVAILABLE) from None
            job.task_id = task.id
        _LOG.info("discussion.queued", extra={"participants": len(chosen), "rounds": round_count})
        return conversation_id, task.id

    def _engine(self, job: _Job) -> DiscussionEngine:
        async def emit(message: DiscussionMessage) -> None:
            await self._emit(job, message)

        return DiscussionEngine(
            job.personas,
            job.topic,
            resolve_provider=self._models.provider_for,
            moderator=self._moderator,
            emit=emit,
            rounds=job.rounds,
            max_rounds=self._max_rounds,
            is_cancelled=lambda: job.token is not None and job.token.is_set,
            call_timeout_seconds=self._call_timeout,
        )

    # ----- posting -----

    async def _post(self, job: _Job, text: str) -> None:
        await asyncio.shield(self._post_inner(job, text))

    async def _post_inner(self, job: _Job, text: str) -> None:
        text = clean_posted_text(text)
        await self._store.append_assistant_message(job.conversation_id, text)
        job.last_posted = text

    async def _emit(self, job: _Job, message: DiscussionMessage) -> None:
        if message.kind is MessageKind.TURN:
            text = format_message(message.speaker, message.text)
            await self._post_recorded(job, text, (message.speaker, message.text))
            self._advance(job)
        elif message.kind is MessageKind.SUMMARY:
            await self._post(job, format_summary(message.text))
            job.summary_posted = True
            job.summary_text = message.text
            self._advance(job)
        elif message.code == StopCode.CANCELLED.value:
            return  # posted once, after the summary, when the task has ended as cancelled
        else:
            code = message.code or "NOTICE"
            await self._post(job, format_notice(message.text, code))
            if code == "PERSONA_SKIPPED":
                self._advance(job, StepStatus.FAILED, "skipped")
            else:
                job.noticed = True

    async def _post_recorded(self, job: _Job, text: str, turn: tuple[str, str]) -> None:
        async def both() -> None:
            await self._post_inner(job, text)
            job.turns.append(turn)

        await asyncio.shield(both())

    # ----- task progress -----

    def _step(self, action: Callable[[], None]) -> None:
        try:
            action()
        except TaskRepositoryError as error:
            _LOG.warning("discussion.progress_failed", extra={"error_type": type(error).__name__})

    def _advance(
        self, job: _Job, status: StepStatus = StepStatus.COMPLETED, note: str | None = None
    ) -> None:
        progress = job.progress
        if progress is None:
            return
        if job.open_step is not None:
            index = job.open_step
            self._step(lambda: progress.finish_step(index, status=status, note=note))
            job.open_step = None
        if job.next_step < job.steps_total:
            index = job.next_step
            self._step(lambda: progress.start_step(index))
            job.open_step = index
            job.next_step += 1

    def _close_steps(self, job: _Job, status: StepStatus) -> None:
        progress = job.progress
        if progress is None:
            return
        if job.open_step is not None:
            index = job.open_step
            self._step(lambda: progress.finish_step(index, status=status))
            job.open_step = None
        while job.next_step < job.steps_total:
            index = job.next_step
            self._step(partial(progress.start_step, index))
            self._step(partial(progress.finish_step, index, status=StepStatus.SKIPPED))
            job.next_step += 1

    # ----- cancel / recover / worker -----

    def cancel(self, task_id: UUID) -> Task:
        """Cancel a discussion task. One that had not started is cancelled here; a running one is
        stopped by the worker. The closing messages are posted by `_finish` either way."""
        task = self.queue.repository.get_task(task_id)
        if task is not None and parse_discussion_goal(task.goal) is None:
            raise NotADiscussionTask
        after = self.queue.cancel(task_id)
        if after.status is TaskStatus.CANCELLED:
            loop = self._loop_ref
            if loop is not None:
                asyncio.run_coroutine_threadsafe(self._finish(after), loop)
        return after

    async def recover(self) -> int:
        """Settle discussions a previous process left running, each with a notice. Only this
        service's own tasks are touched, so run it before the generic recovery."""
        self._loop_ref = asyncio.get_running_loop()
        repository = self.queue.repository
        count = 0
        for task in repository.list_goal_tasks(DISCUSSION_GOAL_PREFIX, TaskStatus.RUNNING):
            try:
                settled = repository.transition(
                    task.id,
                    TaskStatus.RUNNING,
                    TaskStatus.FAILED,
                    failure_code=TaskFailure.INTERRUPTED,
                )
            except TaskStateChanged:
                continue
            count += 1
            await self._finish(settled)
        return count

    async def run_worker(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        self._loop_ref = asyncio.get_running_loop()
        executor = _Executor(self)
        verifier = _Verifier(self)
        while not stop.is_set():
            ran = None
            try:
                ran = await self.queue.run_next(
                    executor,
                    verifier,
                    timeout_seconds=self.task_timeout_seconds,
                    goal_prefix=DISCUSSION_GOAL_PREFIX,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOG.error("discussion.worker_failed", extra={"error_type": type(error).__name__})
            if ran is not None:
                await self._finish(ran)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)

    async def _finish(self, task: Task) -> None:
        """The closing messages of a task that ended without its own: once per discussion."""
        parsed = parse_discussion_goal(task.goal)
        if parsed is None:
            return
        job = self._jobs.get(parsed[1])
        if job is not None:
            if job.finished:
                return
            job.finished = True
        try:
            if task.status is TaskStatus.CANCELLED:
                await self._post_cancelled(parsed[0], job)
            elif task.status is TaskStatus.FAILED and not (job is not None and job.noticed):
                text, code = _FAILURE_NOTICE.get(
                    task.failure_code or TaskFailure.EXECUTION_FAILED,
                    _FAILURE_NOTICE[TaskFailure.EXECUTION_FAILED],
                )
                await self._store.append_assistant_message(parsed[0], format_notice(text, code))
        except ConversationNotFound:
            pass  # the conversation is gone: nothing to post into
        except Exception as error:
            _LOG.warning("discussion.post_failed", extra={"error_type": type(error).__name__})
        finally:
            self._jobs.pop(parsed[1], None)
        if job is not None and task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            await self._remember(parsed[0], job)

    async def _remember(self, conversation_id: UUID, job: _Job) -> None:
        """Hand the summary to the memory hook. Failed, empty and cancelled-without-summary
        discussions have no turn or no summary and are skipped. Never affects the discussion."""
        hook = self._on_summary
        if hook is None or not job.turns or not job.summary_text:
            return
        try:
            await asyncio.to_thread(hook, conversation_id, job.topic, job.summary_text)
        except Exception:  # the hook is best effort; fixed code only, never text
            _LOG.warning("discussion.memory_failed", extra={"code": "memory_hook_failed"})

    async def _post_cancelled(self, conversation_id: UUID, job: _Job | None) -> None:
        if job is not None and job.turns and not job.summary_posted:
            summary = await self._partial_summary(job)
            if summary:
                await self._store.append_assistant_message(conversation_id, format_summary(summary))
                job.summary_text = summary
        text, code = CANCELLED_NOTICE
        await self._store.append_assistant_message(conversation_id, format_notice(text, code))

    async def _partial_summary(self, job: _Job) -> str | None:
        lines = [format_message(speaker, text) for speaker, text in job.turns]
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            if used + len(line) > _PARTIAL_TRANSCRIPT_CHARS and kept:
                break
            kept.append(line)
            used += len(line) + 1
        kept.reverse()
        request = CompletionRequest(
            messages=(
                ChatMessage(role="system", content=MODERATOR_SYSTEM),
                ChatMessage(
                    role="user",
                    content=(
                        f"議題:\n{job.topic}\n\n議論のこれまでの発言:\n"
                        + "\n".join(kept)
                        + f"\n\n{_PARTIAL_INSTRUCTION}{SUMMARY_INSTRUCTION}"
                    ),
                ),
            )
        )
        try:
            response = await asyncio.wait_for(self._moderator.complete(request), self._call_timeout)
        except Exception as error:  # CancelledError propagates; a failed summary is only skipped
            _LOG.warning("discussion.summary_failed", extra={"error_type": type(error).__name__})
            return None
        text = response.text.strip() if isinstance(response.text, str) else ""
        return text[:SUMMARY_MAX_CHARS] or None

    # ----- the job itself -----

    async def run_job(
        self, task: Task, progress: ProgressReporter, cancellation: CancellationToken
    ) -> ExecutionOutcome:
        parsed = parse_discussion_goal(task.goal)
        job = self._jobs.get(parsed[1]) if parsed else None
        if job is None or job.engine is None:
            return ExecutionOutcome.failure()  # for example queued before a restart
        job.token = cancellation
        job.progress = progress
        job.next_step = 0
        job.open_step = None
        self._advance(job)  # starts step 0
        try:
            result = await job.engine.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOG.error("discussion.job_failed", extra={"error_type": type(error).__name__})
            self._close_steps(job, StepStatus.FAILED)
            return ExecutionOutcome.failure()
        done = result.stop is StopCode.COMPLETED and result.summarized
        self._close_steps(job, StepStatus.SKIPPED if done else StepStatus.FAILED)
        return ExecutionOutcome.succeeded(POSTED_SUMMARY) if done else ExecutionOutcome.failure()


class _Executor:
    def __init__(self, service: DiscussionService) -> None:
        self._service = service

    async def execute(
        self, task: Task, progress: ProgressReporter, cancellation: CancellationToken
    ) -> ExecutionOutcome:
        return await self._service.run_job(task, progress, cancellation)


class _Verifier:
    """Passes only when the posted summary really is the conversation's last message."""

    def __init__(self, service: DiscussionService) -> None:
        self._service = service

    async def verify(self, task: Task, outcome: ExecutionOutcome) -> VerificationResult:
        parsed = parse_discussion_goal(task.goal)
        job = self._service._jobs.get(parsed[1]) if parsed else None
        if parsed is None or job is None or job.last_posted is None or not job.summary_posted:
            return VerificationResult(False)
        history = await self._service._store.history(parsed[0])
        if not history:
            return VerificationResult(False)
        last = history[-1]
        return VerificationResult(last.role == "assistant" and last.content == job.last_posted)
