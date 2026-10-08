"""Read-only task state endpoints and a progress event stream.

Everything here reports what the repository has persisted. Nothing is computed
that the database does not hold: no percentages, no estimates, and no task
shown as running unless its stored status says so. There are no write, cancel
or retry routes; those need a separate permission decision.

Task text (goal, step descriptions, notes, result summary) is untrusted data
and is only ever placed inside JSON values. Failure and waiting causes are the
fixed enum values from `backend.tasks.models`, never free text.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

import anyio
from anyio import CancelScope, to_thread
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from backend.tasks.models import TERMINAL_STATUSES, StepStatus, Task, TaskStatus
from backend.tasks.repository import TaskRepository, TaskRepositoryError

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_MAX_STREAM_SECONDS = 300.0
DEFAULT_MAX_STREAMS = 20

#: The only values an SSE `error` event may carry in `code`.
ERROR_STREAM_TIME_LIMIT = "stream_time_limit"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"
ERROR_TASK_NOT_FOUND = "task_not_found"


class StreamLease:
    """One claimed stream slot. Releasing twice is harmless."""

    def __init__(self, slots: "StreamSlots") -> None:
        self._slots: StreamSlots | None = slots

    def release(self) -> None:
        slots, self._slots = self._slots, None
        if slots is not None:
            slots.active -= 1


class StreamSlots:
    """Bounds concurrent event streams. Used from the event loop only."""

    def __init__(self, limit: int = DEFAULT_MAX_STREAMS) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        self.limit = limit
        self.active = 0

    def acquire(self) -> StreamLease | None:
        if self.active >= self.limit:
            return None
        self.active += 1
        return StreamLease(self)


class _LeasedStreamingResponse(StreamingResponse):
    """Always closes the body and returns the slot, even if the stream never started.

    A suspended or never-started async generator would otherwise keep its slot
    until garbage collection.
    """

    def __init__(self, content: AsyncIterator[str], lease: StreamLease, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    self._lease.release()


def _sse(event: str, data: dict[str, Any]) -> str:
    # Same framing as the chat stream. json.dumps escapes newlines and other
    # control characters, so task text can never end or forge an SSE frame.
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def task_summary(task: Task) -> dict[str, Any]:
    """Stored task fields, plus step counts derived from stored step statuses."""
    return {
        "id": str(task.id),
        "goal": task.goal,
        "status": task.status.value,
        "current_step": task.current_step,
        "steps_total": len(task.steps),
        "steps_completed": sum(1 for step in task.steps if step.status is StepStatus.COMPLETED),
        "target_device": task.target_device,
        "attempt": task.attempt,
        "retry_of": str(task.retry_of) if task.retry_of is not None else None,
        "verified": task.verified.value,
        "failure_code": task.failure_code.value if task.failure_code is not None else None,
        "waiting_reason": task.waiting_reason.value if task.waiting_reason is not None else None,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }


def task_detail(task: Task) -> dict[str, Any]:
    detail = task_summary(task)
    detail["result_summary"] = task.result_summary
    detail["steps"] = [
        {
            "index": step.index,
            "description": step.description,
            "status": step.status.value,
            "started_at": _iso(step.started_at),
            "finished_at": _iso(step.finished_at),
            "note": step.note,
        }
        for step in task.steps
    ]
    return detail


def _done_event(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "status": task.status.value,
        "failure_code": task.failure_code.value if task.failure_code is not None else None,
        "verified": task.verified.value,
        "finished_at": _iso(task.finished_at),
    }


def _positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number")


def create_tasks_router(
    repository: TaskRepository,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    max_stream_seconds: float = DEFAULT_MAX_STREAM_SECONDS,
    max_streams: int = DEFAULT_MAX_STREAMS,
    slots: StreamSlots | None = None,
) -> APIRouter:
    """Build the read-only task router.

    Repository calls use short-lived SQLite connections. The plain endpoints
    are sync functions (FastAPI runs them in its thread pool) and the stream
    runs each poll in a worker thread, so a slow disk never blocks the loop.
    """
    _positive(poll_interval_seconds, "poll_interval_seconds")
    _positive(heartbeat_seconds, "heartbeat_seconds")
    _positive(max_stream_seconds, "max_stream_seconds")
    slots = slots if slots is not None else StreamSlots(max_streams)
    router = APIRouter()

    def storage_unavailable(exc: TaskRepositoryError) -> HTTPException:
        _LOG.warning("Task storage failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail="task storage unavailable")

    async def read(function: Callable[..., _T], *args: Any) -> _T:
        return await to_thread.run_sync(function, *args)

    @router.get("/api/tasks")
    def list_tasks(
        status: TaskStatus | None = None,
        limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    ) -> dict[str, Any]:
        try:
            tasks = repository.list_tasks(status, limit=limit)
        except TaskRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        return {"tasks": [task_summary(task) for task in tasks]}

    @router.get("/api/tasks/{task_id}")
    def get_task(task_id: UUID) -> dict[str, Any]:
        try:
            task = repository.get_task(task_id)
        except TaskRepositoryError as exc:
            raise storage_unavailable(exc) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task_detail(task)

    @router.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: UUID) -> StreamingResponse:
        lease = slots.acquire()
        if lease is None:
            raise HTTPException(
                status_code=429,
                detail="too many task streams",
                headers={"Retry-After": "5"},
            )
        try:
            try:
                first = await read(repository.get_task, task_id)
            except TaskRepositoryError as exc:
                raise storage_unavailable(exc) from exc
            if first is None:
                raise HTTPException(status_code=404, detail="task not found")
            response = _LeasedStreamingResponse(
                events(first, lease),
                lease,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except BaseException:
            lease.release()
            raise
        return response

    async def events(first: Task, lease: StreamLease) -> AsyncIterator[str]:
        try:
            last = task_detail(first)
            yield _sse("snapshot", last)
            if first.status in TERMINAL_STATUSES:
                yield _sse("done", _done_event(first))
                return
            started = last_sent = anyio.current_time()
            while True:
                remaining = started + max_stream_seconds - anyio.current_time()
                if remaining <= 0:
                    yield _sse("error", {"code": ERROR_STREAM_TIME_LIMIT})
                    return
                await anyio.sleep(min(poll_interval_seconds, remaining))
                try:
                    current = await read(repository.get_task, first.id)
                except TaskRepositoryError as exc:
                    _LOG.warning("Task stream read failed: %s", type(exc).__name__)
                    yield _sse("error", {"code": ERROR_STORAGE_UNAVAILABLE})
                    return
                if current is None:
                    yield _sse("error", {"code": ERROR_TASK_NOT_FOUND})
                    return
                detail = task_detail(current)
                now = anyio.current_time()
                if detail != last:
                    last, last_sent = detail, now
                    yield _sse("progress", detail)
                    if current.status in TERMINAL_STATUSES:
                        yield _sse("done", _done_event(current))
                        return
                elif now - last_sent >= heartbeat_seconds:
                    last_sent = now
                    yield ": keep-alive\n\n"
        finally:
            lease.release()

    return router
