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
    TaskFailure,
    TaskStatus,
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
CANCELLED_MESSAGE = "タスクが取り消されました。"
_FAILURE_TEXT = {
    TaskFailure.EXECUTION_FAILED: FAILED_MESSAGE,
    TaskFailure.VERIFICATION_FAILED: "結果を会話で確認できなかったため、タスクを失敗にしました。",
    TaskFailure.TIMEOUT: "ツールのタスクが制限時間内に終わらなかったため、中断しました。",
    TaskFailure.INTERRUPTED: "サーバーの再起動などで、ツールのタスクが中断されました。",
    TaskFailure.INTERNAL_ERROR: "内部エラーのため、ツールのタスクを完了できませんでした。",
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
        self._loop_ref: asyncio.AbstractEventLoop | None = None
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
        steps = [
            label
            for i in range(self._max_steps)
            for label in (f"承認待ち {i + 1}", f"ツール実行 {i + 1}")
        ] + ["会話へ投稿"]
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
        """Cancel a chat-tool task. A task that had not started is cancelled here and the fixed
        message is posted; a running one is stopped by the worker, which posts it."""
        task = self.queue.repository.get_task(task_id)
        if task is not None and conversation_of_goal(task.goal) is None:
            raise NotAToolTask
        after = self.queue.cancel(task_id)
        if after.status is TaskStatus.CANCELLED:
            self._post_soon(after)
        return after

    def _post_soon(self, task: Task) -> None:
        loop = self._loop_ref
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._post_ending(task), loop)

    async def _post_ending(self, task: Task) -> None:
        """Fixed message for a cancelled or failed task whose loop did not post its own."""
        parsed = parse_chat_tool_goal(task.goal)
        if parsed is None:
            return
        job = self._jobs.get(parsed[1])
        if job is not None and job.posted is not None:
            return  # the loop already posted its message: never post twice
        if task.status is TaskStatus.CANCELLED:
            text = CANCELLED_MESSAGE
        elif task.status is TaskStatus.FAILED and task.failure_code is not None:
            text = _FAILURE_TEXT.get(task.failure_code, FAILED_MESSAGE)
        else:
            return
        try:
            await self._store.append_assistant_message(parsed[0], text)
        except ConversationNotFound:
            return  # the conversation is gone: nothing to post into
        except Exception as error:
            _LOG.warning("chat_tools.post_failed", extra={"error_type": type(error).__name__})
            return
        if job is not None:
            job.posted = text

    async def recover(self) -> int:
        """Settle tasks a previous process left running; each gets its fixed message."""
        self._loop_ref = asyncio.get_running_loop()
        recovered = self.queue.repository.recover_in_flight()
        for task in recovered:
            await self._post_ending(task)
        return len(recovered)

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
                    goal_prefix=CHAT_TOOL_GOAL_PREFIX,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOG.error("chat_tools.worker_failed", extra={"error_type": type(error).__name__})
            if ran is not None:
                await self._post_ending(ran)
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
        post = 2 * self._max_steps
        if state.open_step is not None:
            progress.finish_step(state.open_step, status=StepStatus.FAILED)
        for index in range(state.next_step, post):
            progress.start_step(index)
            progress.finish_step(index, status=StepStatus.SKIPPED)
        progress.start_step(post)
        text = clean_posted_text(final)
        try:
            await self._store.append_assistant_message(conversation_id, text)
        except ConversationNotFound:
            progress.finish_step(post, status=StepStatus.FAILED, note="not_posted")
            return ExecutionOutcome.failure()
        job.posted = text
        progress.finish_step(post, note="posted")
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
            descriptor = self.toolbox.descriptor(call.name)
            wait_index, run_index = 2 * step, 2 * step + 1
            progress.start_step(wait_index)
            state.open_step = wait_index

            def on_granted(wait_index: int = wait_index, run_index: int = run_index) -> None:
                # The human approved: the waiting step is done, the tool step starts.
                if state.open_step == wait_index:
                    progress.finish_step(wait_index, note="approved")
                    progress.start_step(run_index)
                    state.open_step = run_index

            if descriptor is not None and not descriptor.needs_approval:
                progress.finish_step(wait_index, status=StepStatus.SKIPPED)
                progress.start_step(run_index)
                state.open_step = run_index
            try:
                prepared = self.toolbox.prepare(call.name, call.arguments)
            except ToolRefused:
                result = StepResult("denied", "permission_denied", None)
                prepared = None
            else:
                result = await self.toolbox.execute(
                    prepared, on_granted=on_granted, cancellation=token
                )
                if not result.ok:
                    self.toolbox.discard(prepared)
            if state.open_step == wait_index:
                # Never granted: the wait itself ended (denied, expired, timed out or refused).
                progress.finish_step(
                    wait_index, status=StepStatus.FAILED, note=f"{call.name}: {result.status}"
                )
                state.next_step = run_index  # the tool step is skipped later
            else:
                progress.finish_step(
                    run_index,
                    status=StepStatus.COMPLETED if result.ok else StepStatus.FAILED,
                    note=f"{call.name}: {result.status}",
                )
                state.next_step = run_index + 1
            state.open_step = None
            step += 1
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
