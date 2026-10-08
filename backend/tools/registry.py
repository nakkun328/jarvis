"""Tool registry and the invocation pipeline.

lookup -> argument validation -> permission -> run (timeout, cancellation) -> output validation.
Every outcome is reported to an audit sink as digests and sizes only.
"""

import asyncio
import hashlib
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.tools.contract import (
    DEFAULT_LIMITS,
    CancellationToken,
    PermissionLevel,
    SchemaLimits,
    SchemaViolation,
    Tool,
    ToolCall,
    ToolContext,
    ToolErrorCode,
    ToolResult,
    ToolSpec,
    ToolSpecError,
    ToolStatus,
    canonical_json,
    freeze_json,
    safe_name,
    validate_value,
)
from backend.tools.permission import ConfirmationGrant, PermissionPolicy

logger = logging.getLogger(__name__)

MAX_REGISTERED_TOOLS = 256
DEFAULT_CANCEL_GRACE_SECONDS = 1.0


class DuplicateToolError(ValueError):
    """A tool with this name is already registered."""


@dataclass(frozen=True)
class ToolAuditRecord:
    """Outcome of one invocation. Holds digests and sizes, never argument or output content."""

    timestamp: datetime
    call_id: str
    tool_name: str
    permission: PermissionLevel | None
    status: ToolStatus
    error: ToolErrorCode | None
    permission_reason: str | None
    confirmed: bool
    duration_ms: float
    argument_digest: str | None
    argument_bytes: int | None
    output_digest: str | None
    output_bytes: int | None


class ToolAuditSink(Protocol):
    def record(self, record: ToolAuditRecord) -> None: ...


class InMemoryAuditSink:
    """Bounded in-process sink for tests and local runs."""

    def __init__(self, max_records: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._records: deque[ToolAuditRecord] = deque(maxlen=max_records)

    def record(self, record: ToolAuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> tuple[ToolAuditRecord, ...]:
        with self._lock:
            return tuple(self._records)


@dataclass
class _Entry:
    tool: Tool
    enabled: bool = True


@dataclass
class _Outcome:
    """Mutable scratch for one invocation, turned into a result and an audit record."""

    spec: ToolSpec | None = None
    reason: str | None = None
    confirmed: bool = False
    argument_digest: str | None = None
    argument_bytes: int | None = None
    output_digest: str | None = None
    output_bytes: int | None = None


def _digest_and_size(value: Any) -> tuple[str | None, int | None]:
    try:
        text = canonical_json(value)
    except ValueError:
        return None, None
    data = text.encode("utf-8")
    return hashlib.sha256(data).hexdigest(), len(data)


class ToolRegistry:
    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        *,
        audit: ToolAuditSink | None = None,
        limits: SchemaLimits = DEFAULT_LIMITS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        self._policy = policy if policy is not None else PermissionPolicy()
        self._audit = audit
        self._limits = limits
        self._grace = cancel_grace_seconds
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    # Registration and discovery -------------------------------------------------------

    def register(self, tool: Tool, *, enabled: bool = True) -> None:
        spec = getattr(tool, "spec", None)
        if not isinstance(spec, ToolSpec) or not callable(getattr(tool, "run", None)):
            raise ToolSpecError("a tool needs a ToolSpec `spec` and an async `run` method")
        with self._lock:
            if spec.name in self._entries:
                raise DuplicateToolError(f"tool already registered: {spec.name}")
            if len(self._entries) >= MAX_REGISTERED_TOOLS:
                raise ToolSpecError("too many tools registered")
            self._entries[spec.name] = _Entry(tool, enabled)

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return False
            entry.enabled = enabled
            return True

    def disable(self, name: str) -> bool:
        return self.set_enabled(name, False)

    def enable(self, name: str) -> bool:
        return self.set_enabled(name, True)

    def get(self, name: str) -> Tool | None:
        """The enabled tool with this name, or None."""
        with self._lock:
            entry = self._entries.get(name) if isinstance(name, str) else None
            return entry.tool if entry is not None and entry.enabled else None

    def search(
        self,
        *,
        prefix: str | None = None,
        permission: PermissionLevel | None = None,
        environment: str | None = None,
        text: str | None = None,
    ) -> tuple[ToolSpec, ...]:
        """Specs of enabled tools matching every given filter, sorted by name."""
        with self._lock:
            specs = [e.tool.spec for e in self._entries.values() if e.enabled]
        needle = text.lower() if text else None
        found = [
            s
            for s in specs
            if (prefix is None or s.name.startswith(prefix))
            and (permission is None or s.permission is permission)
            and (environment is None or s.environment == environment)
            and (needle is None or needle in s.name or needle in s.description.lower())
        ]
        return tuple(sorted(found, key=lambda s: s.name))

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return self.search()

    # Invocation -------------------------------------------------------------------------

    async def invoke(
        self,
        call: ToolCall,
        *,
        grant: ConfirmationGrant | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ToolResult:
        """Run one call through the full pipeline; never raises for tool-side failures.

        Permission is evaluated here, against the validated arguments that will actually run,
        so a decision cannot drift from the executed call. A caller-requested cancellation of
        this coroutine propagates as CancelledError after the tool is stopped and audited.
        """
        started = time.monotonic()
        outcome = _Outcome()
        try:
            result = await self._pipeline(call, grant, cancellation, outcome)
        except asyncio.CancelledError:
            self._record(call, outcome, ToolStatus.CANCELLED, ToolErrorCode.CANCELLED, started)
            raise
        except Exception as exc:
            logger.warning("tool pipeline failed: %s", type(exc).__name__)
            result = self._result(call, ToolStatus.ERROR, ToolErrorCode.INTERNAL_ERROR)
        self._record(call, outcome, result.status, result.error, started)
        return result

    async def _pipeline(
        self,
        call: ToolCall,
        grant: ConfirmationGrant | None,
        cancellation: CancellationToken | None,
        outcome: _Outcome,
    ) -> ToolResult:
        with self._lock:
            entry = self._entries.get(call.tool_name) if isinstance(call.tool_name, str) else None
            tool = entry.tool if entry is not None else None
            enabled = entry is not None and entry.enabled
        if tool is None:
            outcome.reason = "unknown_tool"
            return self._result(call, ToolStatus.ERROR, ToolErrorCode.UNKNOWN_TOOL)
        spec = tool.spec
        outcome.spec = spec
        if not enabled:
            outcome.reason = "tool_unavailable"
            return self._result(call, ToolStatus.ERROR, ToolErrorCode.TOOL_UNAVAILABLE)

        arguments = call.arguments
        if not isinstance(arguments, Mapping):
            outcome.reason = "invalid_arguments"
            return self._result(call, ToolStatus.INVALID_ARGUMENTS, ToolErrorCode.INVALID_ARGUMENTS)
        violations = validate_value(spec.input_schema, arguments, limits=self._limits)
        if violations:
            outcome.reason = "invalid_arguments"
            return self._result(
                call,
                ToolStatus.INVALID_ARGUMENTS,
                ToolErrorCode.INVALID_ARGUMENTS,
                violations=violations,
            )
        # No await between validation and the snapshot, so the arguments cannot change here.
        snapshot = freeze_json(arguments)
        outcome.argument_digest, outcome.argument_bytes = _digest_and_size(snapshot)

        frozen_call = ToolCall(call.call_id, call.tool_name, snapshot)
        try:
            decision = self._policy.evaluate(frozen_call, spec, grant=grant)
        except Exception as exc:  # fail closed on a faulty policy
            logger.warning("permission policy failed: %s", type(exc).__name__)
            return self._result(call, ToolStatus.DENIED, ToolErrorCode.PERMISSION_DENIED)
        outcome.reason = str(decision.reason_code)
        if not decision.allowed:
            code = (
                ToolErrorCode.CONFIRMATION_REQUIRED
                if decision.requires_confirmation
                else ToolErrorCode.PERMISSION_DENIED
            )
            return self._result(call, ToolStatus.DENIED, code)
        outcome.confirmed = grant is not None and outcome.reason == "grant_accepted"

        token = cancellation if cancellation is not None else CancellationToken()
        if token.cancelled:
            return self._result(call, ToolStatus.CANCELLED, ToolErrorCode.CANCELLED)
        context = ToolContext(
            call_id=call.call_id,
            tool_name=spec.name,
            permission=spec.permission,
            confirmed=outcome.confirmed,
            timeout_seconds=spec.timeout_seconds,
            cancellation=token,
        )
        return await self._execute(call, tool, spec, snapshot, context, outcome)

    async def _execute(
        self,
        call: ToolCall,
        tool: Tool,
        spec: ToolSpec,
        snapshot: Mapping[str, Any],
        context: ToolContext,
        outcome: _Outcome,
    ) -> ToolResult:
        async def invoke_tool() -> Mapping[str, Any]:
            return await tool.run(snapshot, context)

        runner = asyncio.ensure_future(invoke_tool())
        watcher = asyncio.ensure_future(context.cancellation.wait()) if spec.cancellable else None
        try:
            waiting = {runner} if watcher is None else {runner, watcher}
            done, _ = await asyncio.wait(
                waiting, timeout=spec.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if runner in done:
                return self._finish(call, spec, runner, outcome)
            await self._stop(runner)
            if watcher is not None and watcher in done:
                return self._result(call, ToolStatus.CANCELLED, ToolErrorCode.CANCELLED)
            return self._result(call, ToolStatus.TIMEOUT, ToolErrorCode.TIMEOUT)
        finally:
            if not runner.done():
                runner.cancel()
                runner.add_done_callback(_consume)
            if watcher is not None:
                watcher.cancel()

    async def _stop(self, runner: "asyncio.Future[Any]") -> None:
        runner.cancel()
        await asyncio.wait({runner}, timeout=self._grace)
        if not runner.done():
            logger.warning("tool ignored cancellation within the grace period")
        runner.add_done_callback(_consume)

    def _finish(
        self, call: ToolCall, spec: ToolSpec, runner: "asyncio.Future[Any]", outcome: _Outcome
    ) -> ToolResult:
        if runner.cancelled() or runner.exception() is not None:
            exc = None if runner.cancelled() else runner.exception()
            logger.warning(
                "tool %s failed: %s", spec.name, type(exc).__name__ if exc else "CancelledError"
            )
            return self._result(call, ToolStatus.ERROR, ToolErrorCode.INTERNAL_ERROR)
        output = runner.result()
        if not isinstance(output, Mapping):
            return self._result(call, ToolStatus.ERROR, ToolErrorCode.INVALID_OUTPUT)
        violations = validate_value(spec.output_schema, output, limits=self._limits)
        if violations:
            return self._result(
                call, ToolStatus.ERROR, ToolErrorCode.INVALID_OUTPUT, violations=violations
            )
        frozen = freeze_json(output)
        outcome.output_digest, outcome.output_bytes = _digest_and_size(frozen)
        return ToolResult(call.call_id, spec.name, ToolStatus.OK, output=frozen)

    @staticmethod
    def _result(
        call: ToolCall,
        status: ToolStatus,
        error: ToolErrorCode,
        *,
        violations: tuple[SchemaViolation, ...] = (),
    ) -> ToolResult:
        return ToolResult(
            call.call_id, safe_name(call.tool_name), status, error=error, violations=violations
        )

    def _record(
        self,
        call: ToolCall,
        outcome: _Outcome,
        status: ToolStatus,
        error: ToolErrorCode | None,
        started: float,
    ) -> None:
        if self._audit is None:
            return
        record = ToolAuditRecord(
            timestamp=datetime.now(UTC),
            call_id=call.call_id,
            tool_name=safe_name(call.tool_name),
            permission=outcome.spec.permission if outcome.spec else None,
            status=status,
            error=error,
            permission_reason=outcome.reason,
            confirmed=outcome.confirmed,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            argument_digest=outcome.argument_digest,
            argument_bytes=outcome.argument_bytes,
            output_digest=outcome.output_digest,
            output_bytes=outcome.output_bytes,
        )
        try:
            self._audit.record(record)
        except Exception as exc:  # an audit failure must not change the call's outcome
            logger.error("tool audit sink failed: %s", type(exc).__name__)


def _consume(future: "asyncio.Future[Any]") -> None:
    """Mark a stopped tool task's exception as retrieved so it is not logged at GC."""
    if not future.cancelled():
        future.exception()
