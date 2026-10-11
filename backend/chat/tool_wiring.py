"""Builds the tools a chat turn may use (opt-in, ``JARVIS_CHAT_TOOLS``; docs/chat-tools.md).

This is the only chat module that touches the tool registry or the confirming executor. It
registers the read-only filesystem tools, the write tools (only with ``JARVIS_TOOLS_FS_WRITE``)
and the shell tools (only with ``JARVIS_SHELL_ENABLED``), all behind ONE permission policy, and
hands the chat loop a small ``ChatToolbox``:

- what the model may be told about each tool (built from the registry, by code),
- how a model-written call becomes a real ``ToolCall`` (text for a write goes to the content
  stage; the call carries only its hash and size),
- running a call through the confirming executor, which queues a human approval for anything
  Yellow or Red and waits (bounded) for the decision.

The toolbox holds an ``ApprovalRequester``. It can ask for an approval, read its state and use an
approved one once; it has no way to approve or deny. Nothing the model writes reaches the
approval API.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.core.config import Settings
from backend.tools.approvals import ApprovalRequest, ApprovalRequester, ApprovalState
from backend.tools.confirmed import ApprovalOutcome, ConfirmedExecutor
from backend.tools.contract import (
    CancellationToken,
    PermissionLevel,
    ToolCall,
    ToolErrorCode,
    ToolSpec,
    canonical_json,
    validate_value,
)
from backend.tools.filesystem import (
    FilesystemRoot,
    FilesystemToolError,
    filesystem_scope_checks,
    register_readonly_filesystem_tools,
)
from backend.tools.filesystem_write import (
    ContentStage,
    filesystem_write_tools,
)
from backend.tools.permission import PermissionPolicy
from backend.tools.registry import ToolAuditSink, ToolRegistry
from backend.tools.shell_policy import ShellRunSink, build_shell_wiring, validate_root

_LOG = logging.getLogger(__name__)

#: The one name the model can use for the root, for files and for the shell's cwd.
ROOT_NAME = "work"
#: Cap on the text a model may put into one write (characters). The tools allow more.
MAX_CONTENT_CHARS = 32_768
DEFAULT_POLL_SECONDS = 1.0

#: Staged-content argument pair of the write tools, replaced by one `content` text for the model.
_STAGED = ("content_sha256", "content_size")


class Ending(StrEnum):
    """A human decision (or its absence) that ends the whole task."""

    DENIED = "denied"
    EXPIRED = "expired"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class StepResult:
    """One finished call, reduced to fixed codes plus the (untrusted) output."""

    status: str  # a ToolStatus value
    error: str | None  # a ToolErrorCode value
    output: Mapping[str, Any] | None
    ending: Ending | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _step_result(outcome: ApprovalOutcome) -> StepResult:
    result = outcome.result
    ending: Ending | None = None
    if outcome.approval_id is not None and not result.status.value == "ok":
        if outcome.approval_state is ApprovalState.DENIED:
            ending = Ending.DENIED
        elif outcome.approval_state is ApprovalState.EXPIRED:
            ending = Ending.EXPIRED
        elif outcome.approval_state in (ApprovalState.PENDING, None) and (
            result.error is ToolErrorCode.CONFIRMATION_REQUIRED
        ):
            ending = Ending.TIMEOUT
    return StepResult(
        result.status.value,
        result.error.value if result.error is not None else None,
        result.output,
        ending,
    )


class ToolRefused(Exception):
    """A call that cannot even be queued (for example the content stage is full)."""


@dataclass(frozen=True)
class ToolDescriptor:
    """What the model is told about one registered tool. Built from the registry."""

    name: str
    description: str
    permission: PermissionLevel
    schema: Mapping[str, Any]  # the model-facing input schema (`content` instead of the hash)
    takes_content: bool

    @property
    def needs_approval(self) -> bool:
        """Every Yellow and Red call waits for a human. No tool is ever auto-allowed here."""
        return self.permission is not PermissionLevel.GREEN


def _thaw(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _descriptor(spec: ToolSpec) -> ToolDescriptor:
    schema = _thaw(spec.input_schema)
    properties = schema.get("properties", {})
    takes_content = all(name in properties for name in _STAGED)
    if takes_content:
        for name in _STAGED:
            del properties[name]
        properties["content"] = {
            "type": "string",
            "maxLength": MAX_CONTENT_CHARS,
            "description": "The full text to write. The system stores it; you do not hash it.",
        }
        schema["required"] = [n for n in schema.get("required", []) if n not in _STAGED] + [
            "content"
        ]
    return ToolDescriptor(spec.name, spec.description, spec.permission, schema, takes_content)


@dataclass(frozen=True)
class PreparedCall:
    call: ToolCall
    descriptor: ToolDescriptor
    staged_sha256: str | None = None


class _WatchingRequester(ApprovalRequester):
    """The same narrow view, plus a callback when an approval has been used (granted)."""

    def __init__(self, inner: ApprovalRequester, on_granted: Callable[[], None]) -> None:
        self._inner = inner
        self._on_granted = on_granted

    def request(self, call: ToolCall) -> ApprovalRequest:
        return self._inner.request(call)

    def state_of(self, approval_id: str) -> ApprovalState | None:
        return self._inner.state_of(approval_id)

    def consume(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        used = self._inner.consume(tool_name, arguments)
        if used:
            self._on_granted()
        return used


class ChatToolbox:
    def __init__(
        self,
        registry: ToolRegistry,
        requester: ApprovalRequester,
        *,
        stage: ContentStage | None,
        approval_timeout_seconds: float,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._requester = requester
        self._stage = stage
        self._timeout = approval_timeout_seconds
        self._poll = poll_seconds
        self._sleep = sleep
        specs = registry.list_specs()
        self._descriptors = {spec.name: _descriptor(spec) for spec in specs}

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._descriptors[name] for name in sorted(self._descriptors))

    def descriptor(self, name: str) -> ToolDescriptor | None:
        return self._descriptors.get(name) if isinstance(name, str) else None

    def check_arguments(self, name: str, arguments: Mapping[str, Any]) -> bool:
        """Whether `arguments` fit the model-facing schema of tool `name` (strict, no extras)."""
        descriptor = self.descriptor(name)
        if descriptor is None or not isinstance(arguments, dict):
            return False
        return not validate_value(descriptor.schema, arguments)

    def prepare(self, name: str, arguments: Mapping[str, Any]) -> PreparedCall:
        """The real call for a checked model call. Write text goes to the stage here."""
        descriptor = self._descriptors[name]
        real = dict(arguments)
        digest: str | None = None
        if descriptor.takes_content:
            if self._stage is None:
                raise ToolRefused("no content stage")
            text = real.pop("content")
            try:
                staged = self._stage.put(text)
            except (TypeError, ValueError):
                raise ToolRefused("content not accepted") from None
            real["content_sha256"] = staged.content_sha256
            real["content_size"] = staged.content_size
            digest = staged.content_sha256
        return PreparedCall(ToolCall(f"chat-{uuid4().hex[:24]}", name, real), descriptor, digest)

    def discard(self, prepared: PreparedCall) -> None:
        if prepared.staged_sha256 is not None and self._stage is not None:
            self._stage.discard(prepared.staged_sha256)

    async def execute(
        self,
        prepared: PreparedCall,
        *,
        on_granted: Callable[[], None] = lambda: None,
        cancellation: CancellationToken | None = None,
    ) -> "StepResult":
        """Run the call. Yellow and Red calls queue a human approval and wait up to the timeout;
        a fresh executor per call keeps `on_granted` private to this call. Cancelling the
        awaiting coroutine leaves the approval request pending (it expires on its own)."""
        executor = ConfirmedExecutor(
            self._registry,
            _WatchingRequester(self._requester, on_granted),
            poll_seconds=self._poll,
            sleep=self._sleep,
        )
        outcome = await executor.execute(
            prepared.call, wait_seconds=self._timeout, cancellation=cancellation
        )
        return _step_result(outcome)


def effective_shell_root(settings: Settings) -> Path | None:
    """The shell root: JARVIS_SHELL_ROOT if set, else the chat tools root."""
    return settings.shell_root if settings.shell_root is not None else settings.tools_root


def build_chat_toolbox(
    settings: Settings,
    requester: ApprovalRequester,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    approval_timeout_seconds: float | None = None,
    tool_audit: ToolAuditSink | None = None,
    run_sink: ShellRunSink | None = None,
) -> ChatToolbox | None:
    """The toolbox, or None when chat tools are off or nothing usable could be registered."""
    if not settings.chat_tools:
        return None
    root_path = validate_root(settings.tools_root)
    if root_path is None:
        _LOG.warning("chat_tools.root_invalid")
        return None
    root = FilesystemRoot(ROOT_NAME, Path(root_path))
    checks = dict(filesystem_scope_checks([root]))
    write = None
    stage: ContentStage | None = None
    try:
        if settings.tools_fs_write:
            write = filesystem_write_tools([root])
            stage = write.stage
            checks.update(write.scope_checks)
    except FilesystemToolError:
        _LOG.warning("chat_tools.fs_write_unavailable")
        write = None
        stage = None
    base = PermissionPolicy(scope_checks=checks)
    shell_settings = replace(settings, shell_root=effective_shell_root(settings))
    wiring = build_shell_wiring(
        shell_settings, approvals=requester, tool_audit=tool_audit, base_policy=base,
        run_sink=run_sink,
    )
    registry = wiring.registry if wiring is not None else ToolRegistry(base, audit=tool_audit)
    try:
        register_readonly_filesystem_tools(registry, [root])
    except FilesystemToolError:
        _LOG.warning("chat_tools.fs_unavailable")
    if write is not None:
        for tool in write.tools:
            registry.register(tool)
    if not registry.list_specs():
        _LOG.warning("chat_tools.no_tools")
        return None
    timeout = (
        float(settings.chat_tools_approval_timeout_seconds)
        if approval_timeout_seconds is None
        else approval_timeout_seconds
    )
    _LOG.info("chat_tools.ready", extra={"tools": len(registry.list_specs())})
    return ChatToolbox(
        registry,
        requester,
        stage=stage,
        approval_timeout_seconds=timeout,
        poll_seconds=poll_seconds,
    )


__all__ = [
    "MAX_CONTENT_CHARS",
    "ROOT_NAME",
    "ChatToolbox",
    "Ending",
    "PreparedCall",
    "StepResult",
    "ToolDescriptor",
    "ToolRefused",
    "build_chat_toolbox",
    "effective_shell_root",
]
