"""Tool use as a background task (docs/chat-tools.md).

A chat turn never waits for a tool or for a human. The turn asks the model once; if the reply is
a valid tool call, the turn queues a task (goal: a fixed label, the conversation id and a job id,
never user text), answers with a fixed Japanese message and ends. A single worker then runs the
loop: Green tools run, Yellow/Red tools wait for a human approval (bounded), every result goes
back to the model as quoted data, and when the loop ends the final answer (or a fixed message)
is posted into the original conversation as an assistant message.

The task completes only if a separate verifier finds the posted text as the last message of the
conversation. Denial, expiry, an approval timeout and the step limit post a fixed message and end
the task as failed (no retry that would ask the same approval again). Nothing in this module can
approve: it only holds the toolbox, whose approval handle can request and consume, never decide.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from backend.chat.context import ConversationNotFound, ConversationStore, clean_posted_text
from backend.chat.tool_protocol import (
    BUSY_REPLY,
    DENIED_MESSAGE,
    EXPIRED_MESSAGE,
    FAILED_MESSAGE,
    STARTED_REPLY,
    STOPPED_MESSAGE,
    TIMEOUT_MESSAGE,
    ParsedCall,
    call_text,
    parse_call,
    render_rules,
    result_text,
)
from backend.chat.tool_wiring import ChatToolbox, Ending, StepResult, ToolRefused
from backend.providers.base import ChatMessage, CompletionRequest, LLMProvider, ProviderError
from backend.tasks.models import (
    CHAT_TOOL_GOAL_PREFIX,
    TERMINAL_STATUSES,
    StepStatus,
    Task,
    chat_tool_goal,
    conversation_of_goal,
    parse_chat_tool_goal,
)
from backend.tasks.queue import (
    CancellationToken,
    ExecutionOutcome,
    ProgressReporter,
    TaskQueue,
    VerificationResult,
)
from backend.tools.contract import CancellationToken as ToolCancellation

_LOG = logging.getLogger(__name__)

#: How many chat-tool tasks may be queued or running at once. They run one at a time.
MAX_PENDING_JOBS = 3
DEFAULT_WORKER_POLL_SECONDS = 1.0
_ENDINGS = {
    Ending.DENIED: DENIED_MESSAGE,
    Ending.EXPIRED: EXPIRED_MESSAGE,
    Ending.TIMEOUT: TIMEOUT_MESSAGE,
}
POSTED_SUMMARY = "ツールの結果を会話に投稿しました。"


class ToolBusy(Exception):
    """Too many tool tasks are queued or running."""


class NotAToolTask(Exception):
    """The task is not a chat-tool task."""


@dataclass(frozen=True)
class ToolRequest:
    """The model's first reply was a valid tool call."""

    call: ParsedCall


@dataclass
class _Progress:
    next_step: int = 0
    open_step: int | None = None


@dataclass
class _Job:
    provider: LLMProvider
    messages: tuple[ChatMessage, ...]
    call: ParsedCall
    task_id: UUID | None = None
    posted: str | None = field(default=None)


class ChatToolService:
    def __init__(
        self,
        toolbox: ChatToolbox,
        queue: TaskQueue,
        store: ConversationStore,
        *,
        max_steps: int,
        approval_timeout_seconds: float,
        max_pending: int = MAX_PENDING_JOBS,
        worker_poll_seconds: float = DEFAULT_WORKER_POLL_SECONDS,
    ) -> None:
        self.toolbox = toolbox
        self.queue = queue
        self._store = store
        self._max_steps = max_steps
        self._timeout = approval_timeout_seconds
        self._max_pending = max_pending
        self._poll = worker_poll_seconds
        self._jobs: dict[str, _Job] = {}
        self.rules = render_rules(toolbox.descriptors, max_steps)
        self.task_timeout_seconds = max_steps * (approval_timeout_seconds + 90.0) + 120.0

    # ----- the chat turn side -----

    async def first_step(
        self, provider: LLMProvider, request: CompletionRequest, *, stream: bool
    ) -> AsyncIterator[str | ToolRequest]:
        """One model call. Plain text is yielded as it arrives; a reply that begins with `{` is
        held back until it is complete and then is either a `ToolRequest` or (when it is not a
        valid call) the final answer as one piece of text."""
        held: list[str] = []
        deciding = True
        streaming = False
        async for delta in _model_text(provider, request, stream):
            if not delta:
                continue
            if streaming:
                yield delta
                continue
            held.append(delta)
            head = "".join(held).lstrip()
            if deciding and head:
                deciding = False
                if not head.startswith("{"):
                    streaming = True
                    yield "".join(held)
                    held.clear()
        text = "".join(held)
        if streaming:
            return
        if not text.strip():
            raise ProviderError("Provider returned no text")
        call = parse_call(text, self.toolbox)
        if call is not None:
            yield ToolRequest(call)
        else:
            yield text

    def submit(
        self,
        conversation_id: UUID,
        provider: LLMProvider,
        request: CompletionRequest,
        call: ParsedCall,
    ) -> UUID:
        """Queue the tool task for this conversation. Raises `ToolBusy` at the cap."""
        self._prune()
        if len(self._jobs) >= self._max_pending:
            raise ToolBusy
        job_id = uuid4().hex
        job = _Job(provider, request.messages, call)
        steps = [f"ツール実行 {i + 1}" for i in range(self._max_steps)] + ["会話へ投稿"]
        self._jobs[job_id] = job
        try:
            task = self.queue.submit(chat_tool_goal(conversation_id, job_id), steps)
        except Exception:
            self._jobs.pop(job_id, None)
            raise
        job.task_id = task.id
        return task.id

    started_reply = STARTED_REPLY
    busy_reply = BUSY_REPLY

    def _prune(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.task_id is None:
                continue
            try:
                task = self.queue.repository.get_task(job.task_id)
            except Exception:
                continue
            if task is None or task.status in TERMINAL_STATUSES:
                self._jobs.pop(job_id, None)

    # ----- cancel / recover / worker -----

    def cancel(self, task_id: UUID) -> Task:
        task = self.queue.repository.get_task(task_id)
        if task is not None and conversation_of_goal(task.goal) is None:
            raise NotAToolTask
        return self.queue.cancel(task_id)

    def recover(self) -> int:
        return len(self.queue.repository.recover_in_flight())

    async def run_worker(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        executor = _Executor(self)
        verifier = _Verifier(self)
        while not stop.is_set():
            ran = None
            try:
                ran = await self.queue.run_next(
                    executor,
                    verifier,
                    timeout_seconds=self.task_timeout_seconds,
                    goal_prefix=CHAT_TOOL_GOAL_PREFIX,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOG.error("chat_tools.worker_failed", extra={"error_type": type(error).__name__})
            if ran is not None:
                parsed = parse_chat_tool_goal(ran.goal)
                if parsed is not None:
                    self._jobs.pop(parsed[1], None)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)

    # ----- the loop itself -----

    async def run_job(
        self, task: Task, progress: ProgressReporter
    ) -> ExecutionOutcome:
        parsed = parse_chat_tool_goal(task.goal)
        job = self._jobs.get(parsed[1]) if parsed else None
        if parsed is None or job is None:
            return ExecutionOutcome.failure()  # for example queued before a restart
        conversation_id = parsed[0]
        token = ToolCancellation()
        state = _Progress()
        try:
            final, ended = await self._loop(job, progress, token, state)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            final, ended = FAILED_MESSAGE, True
        except Exception as error:
            _LOG.error("chat_tools.job_failed", extra={"error_type": type(error).__name__})
            final, ended = FAILED_MESSAGE, True
        finally:
            token.cancel()
        # Unused tool steps are skipped; the last step posts to the conversation. A step that
        # was started but not finished (a failure inside it) is closed as failed first.
        if state.open_step is not None:
            progress.finish_step(state.open_step, status=StepStatus.FAILED)
        for index in range(state.next_step, self._max_steps):
            progress.start_step(index)
            progress.finish_step(index, status=StepStatus.SKIPPED)
        progress.start_step(self._max_steps)
        text = clean_posted_text(final)
        try:
            await self._store.append_assistant_message(conversation_id, text)
        except ConversationNotFound:
            progress.finish_step(self._max_steps, status=StepStatus.FAILED, note="not_posted")
            return ExecutionOutcome.failure()
        job.posted = text
        progress.finish_step(self._max_steps, note="posted")
        return ExecutionOutcome.failure() if ended else ExecutionOutcome.succeeded(POSTED_SUMMARY)

    async def _loop(
        self, job: _Job, progress: ProgressReporter, token: ToolCancellation, state: "_Progress"
    ) -> tuple[str, bool]:
        """(text to post, whether the loop ended without a model answer)."""
        messages = list(job.messages)
        call = job.call
        step = 0
        while True:
            if step >= self._max_steps:
                return STOPPED_MESSAGE, True
            progress.start_step(step)
            state.open_step = step
            try:
                prepared = self.toolbox.prepare(call.name, call.arguments)
            except ToolRefused:
                result = StepResult("denied", "permission_denied", None)
                prepared = None
            else:
                result = await self.toolbox.execute(prepared, cancellation=token)
                if not result.ok:
                    self.toolbox.discard(prepared)
            progress.finish_step(
                step,
                status=StepStatus.COMPLETED if result.ok else StepStatus.FAILED,
                note=f"{call.name}: {result.status}",
            )
            state.open_step = None
            step += 1
            state.next_step = step
            if result.ending is not None:
                return _ENDINGS[result.ending], True
            messages.append(ChatMessage(role="assistant", content=call_text(call)))
            messages.append(
                ChatMessage(
                    role="user",
                    content=result_text(call.name, result.status, result.error, result.output),
                )
            )
            response = await job.provider.complete(CompletionRequest(tuple(messages)))
            if not response.text.strip():
                raise ProviderError("Provider returned no text")
            call_next = parse_call(response.text, self.toolbox)
            if call_next is None:
                return response.text, False
            call = call_next


async def _model_text(
    provider: LLMProvider, request: CompletionRequest, stream: bool
) -> AsyncIterator[str]:
    if not stream:
        response = await provider.complete(request)
        yield response.text
        return
    deltas = provider.stream(request)
    try:
        async for delta in deltas:
            yield delta
    finally:
        close = getattr(deltas, "aclose", None)
        if close is not None:
            await close()


class _Executor:
    def __init__(self, service: ChatToolService) -> None:
        self._service = service

    async def execute(
        self, task: Task, progress: ProgressReporter, cancellation: CancellationToken
    ) -> ExecutionOutcome:
        return await self._service.run_job(task, progress)


class _Verifier:
    """Passes only when the posted text really is the conversation's last message."""

    def __init__(self, service: ChatToolService) -> None:
        self._service = service

    async def verify(self, task: Task, outcome: ExecutionOutcome) -> VerificationResult:
        parsed = parse_chat_tool_goal(task.goal)
        job = self._service._jobs.get(parsed[1]) if parsed else None
        if parsed is None or job is None or job.posted is None:
            return VerificationResult(False)
        history = await self._service._store.history(parsed[0])
        if not history:
            return VerificationResult(False)
        last = history[-1]
        return VerificationResult(last.role == "assistant" and last.content == job.posted)
