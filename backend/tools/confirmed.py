"""Executor wrapper: run confirm-required tools only after a human approved the exact call.

It sits in front of `ToolRegistry.invoke`. The registry and permission policy stay the single
source of truth for what needs confirmation; this wrapper only turns "confirmation required" into
an approval request and, once a human approved, into a one-time `ConfirmationGrant`.

The wrapper holds an `ApprovalRequester`, which cannot approve. Nothing the model produces, and no
tool, receives a handle that can decide.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.tools.approvals import (
    ApprovalQueueFull,
    ApprovalRequester,
    ApprovalState,
    ApprovalStoreError,
)
from backend.tools.contract import (
    CancellationToken,
    ToolCall,
    ToolErrorCode,
    ToolResult,
    ToolStatus,
    freeze_json,
    safe_name,
)
from backend.tools.permission import ConfirmationGrant
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

GRANT_LIFETIME = timedelta(seconds=30)
DEFAULT_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class ApprovalOutcome:
    """The tool result plus, when a human decision was involved, which request it concerned."""

    result: ToolResult
    approval_id: str | None = None
    approval_state: ApprovalState | None = None


class ConfirmedExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        approvals: ApprovalRequester,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._approvals = approvals
        self._poll = poll_seconds
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        call: ToolCall,
        *,
        wait_seconds: float = 0.0,
        cancellation: CancellationToken | None = None,
    ) -> ApprovalOutcome:
        """Run `call`; if it needs a human, queue a request and report "confirmation required".

        With `wait_seconds` > 0 the call waits that long for a decision. No decision in time
        means the call does not run (the request stays pending until it expires). The arguments
        are frozen once, so the digest that was approved is the digest that runs.
        """
        try:
            frozen = ToolCall(call.call_id, call.tool_name, freeze_json(call.arguments))
        except (TypeError, ValueError, AttributeError):
            return ApprovalOutcome(await self._registry.invoke(call, cancellation=cancellation))

        first = await self._registry.invoke(frozen, cancellation=cancellation)
        if first.error is not ToolErrorCode.CONFIRMATION_REQUIRED:
            return ApprovalOutcome(first)

        if self._approvals.consume(frozen.tool_name, frozen.arguments):
            return ApprovalOutcome(await self._run_granted(frozen, cancellation))

        try:
            request = self._approvals.request(frozen)
        except ApprovalQueueFull:
            return ApprovalOutcome(_refusal(frozen, ToolErrorCode.PERMISSION_DENIED))
        except (ApprovalStoreError, ValueError):
            return ApprovalOutcome(_refusal(frozen, ToolErrorCode.PERMISSION_DENIED))

        deadline = self._clock() + timedelta(seconds=max(0.0, wait_seconds))
        state = request.state
        while True:
            try:
                current = self._approvals.state_of(request.id)
            except ApprovalStoreError:
                return ApprovalOutcome(
                    _refusal(frozen, ToolErrorCode.PERMISSION_DENIED), request.id, None
                )
            state = current if current is not None else ApprovalState.EXPIRED
            if state is ApprovalState.APPROVED:
                if self._approvals.consume(frozen.tool_name, frozen.arguments):
                    return ApprovalOutcome(
                        await self._run_granted(frozen, cancellation), request.id, state
                    )
                # Approved, but someone else used it first: do not run.
                return ApprovalOutcome(
                    _refusal(frozen, ToolErrorCode.CONFIRMATION_REQUIRED), request.id, state
                )
            if state in (ApprovalState.DENIED, ApprovalState.EXPIRED):
                return ApprovalOutcome(
                    _refusal(frozen, ToolErrorCode.PERMISSION_DENIED), request.id, state
                )
            if self._clock() >= deadline:
                return ApprovalOutcome(
                    _refusal(frozen, ToolErrorCode.CONFIRMATION_REQUIRED), request.id, state
                )
            await self._sleep(self._poll)

    async def _run_granted(
        self, call: ToolCall, cancellation: CancellationToken | None
    ) -> ToolResult:
        grant = ConfirmationGrant.for_call(call, self._clock() + GRANT_LIFETIME)
        return await self._registry.invoke(call, grant=grant, cancellation=cancellation)


def _refusal(call: ToolCall, code: ToolErrorCode) -> ToolResult:
    return ToolResult(call.call_id, safe_name(call.tool_name), ToolStatus.DENIED, error=code)
