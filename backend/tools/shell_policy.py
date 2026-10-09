"""Policy, settings and wiring for the structured shell tool (docs/tool-shell.md).

`backend/tools/shell.py` supplies the mechanism (argument grammar, no-shell execution, caps). This
module decides what an installation may actually register:

- `DEFAULT_ALLOWLIST`: an explicit table of command name -> permission level -> builder. Nothing
  outside the table can be enabled; `JARVIS_SHELL_COMMANDS` only picks entries of it.
- `inspect_shell`: the pure status check behind `python -m backend.doctor` (OFF / OK / INCOMPLETE).
  It returns fixed codes and command names only, never a path.
- `build_shell_wiring`: the one factory. It returns `None` unless the shell is enabled and the
  execution root is valid, and otherwise a private registry whose permission policy carries the
  shell scope checks, plus a `ConfirmedExecutor`. Green commands run directly; Yellow commands
  run only after a human approved that exact call. There is no way to auto-allow Yellow here.
- `ShellRunAudit`: one record per run with a redacted argv summary, exit code and duration, but
  never any output content.
"""

import json
import logging
import os
import re
import sys
import threading
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from backend.core.config import Settings
from backend.tools.approvals import REDACTED, ApprovalRequester
from backend.tools.confirmed import ConfirmedExecutor
from backend.tools.contract import PermissionLevel, ToolContext, ToolSpec
from backend.tools.permission import PermissionPolicy
from backend.tools.registry import ToolAuditSink, ToolRegistry
from backend.tools.shell import (
    AllowedCommand,
    ArgContext,
    ArgumentPolicy,
    CommandCatalog,
    FlagRule,
    ShellAuditRecord,
    ShellConfigError,
    ShellOutcome,
    ShellRejection,
    ShellTool,
    ShellUnsupportedPlatformError,
    build_shell_tools,
    git_readonly_command,
    pytest_command,
    shell_scope_checks,
)
from backend.tools.shell import RelPath as _RelPath

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("jarvis.shell.audit")

ROOT_LABEL = "work"  # the only cwd root name the model can use
MAX_SUMMARY_ARGS = 6
MAX_SUMMARY_ARG_CHARS = 48

# status codes (also the doctor reason codes)
READY = "READY"
NOT_CONFIGURED = "NOT_CONFIGURED"
ROOT_MISSING = "SHELL_ROOT_MISSING"
ROOT_INVALID = "SHELL_ROOT_INVALID"
COMMAND_UNKNOWN = "SHELL_COMMAND_UNKNOWN"
NO_COMMANDS = "SHELL_NO_COMMANDS"
UNSUPPORTED = "SHELL_UNSUPPORTED"
POLICY_INVALID = "SHELL_POLICY_INVALID"


# Allowlist table ------------------------------------------------------------------------------

_SENSITIVE_NAME = re.compile(
    r"(?i)^(\.env.*|\.netrc|\.npmrc|\.pypirc|\.git-credentials|id_(rsa|dsa|ecdsa|ed25519).*"
    r"|.*\.(pem|key|p12|pfx|kdbx|keystore|jks)|.*(secret|credential|password|token).*)$"
)
_SENSITIVE_DIRS = frozenset({".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker"})


class ReadableFile:
    """A path for `cat`: inside the cwd (symlinks resolved), an existing regular file, and not a
    secret-looking or repository-internal file. Refusing is cheap; a false refusal costs little."""

    def accepts(self, value: str, context: ArgContext) -> bool:
        if not _RelPath().accepts(value, context) or context.cwd is None:
            return False
        parts = [p for p in value.split("/") if p not in ("", ".")]
        if not parts:
            return False
        for part in parts:
            if part.lower() in _SENSITIVE_DIRS or _SENSITIVE_NAME.fullmatch(part):
                return False
        real = os.path.realpath(os.path.join(context.cwd, value))
        rel = os.path.relpath(real, os.path.realpath(context.cwd))
        for part in rel.split(os.sep):  # the target of a symlink may hide behind an innocent name
            if part.lower() in _SENSITIVE_DIRS or _SENSITIVE_NAME.fullmatch(part):
                return False
        return os.path.isfile(real)


def _ls(exe: str, roots: Mapping[str, str], timeout: float, cap: int) -> AllowedCommand:
    """Green: flags only, so it can only list the directory the call already is in."""
    policy = ArgumentPolicy(flags=(FlagRule(pattern=r"-[1lAahFtr]{1,6}", max_uses=4),))
    return AllowedCommand(
        name="ls",
        executable=exe,
        permission=PermissionLevel.GREEN,
        read_only=True,
        argument_policy=policy,
        cwd_roots=roots,
        timeout_seconds=timeout,
        max_output_bytes=cap,
        env_fixed={"LC_ALL": "C"},
    )


def _cat(exe: str, roots: Mapping[str, str], timeout: float, cap: int) -> AllowedCommand:
    policy = ArgumentPolicy(
        flags=(FlagRule(name="-n"),), positionals=ReadableFile(), max_positionals=4
    )
    return AllowedCommand(
        name="cat",
        executable=exe,
        permission=PermissionLevel.YELLOW,
        argument_policy=policy,
        cwd_roots=roots,
        timeout_seconds=timeout,
        max_output_bytes=cap,
        env_fixed={"LC_ALL": "C"},
    )


def _git(exe: str, roots: Mapping[str, str], timeout: float, cap: int) -> AllowedCommand:
    return git_readonly_command(
        exe, roots, name="git", timeout_seconds=timeout, max_output_bytes=cap
    )


def _pytest(exe: str, roots: Mapping[str, str], timeout: float, cap: int) -> AllowedCommand:
    return pytest_command(exe, roots, name="pytest", timeout_seconds=timeout, max_output_bytes=cap)


Builder = Callable[[str, Mapping[str, str], float, int], AllowedCommand]


@dataclass(frozen=True)
class CommandEntry:
    """One row of the allowlist table: who may run, at which level, found where."""

    name: str
    level: PermissionLevel
    executables: tuple[str, ...]
    build: Builder

    def __post_init__(self) -> None:
        if self.level is PermissionLevel.RED:
            raise ShellConfigError("the shell allowlist holds Green and Yellow commands only")
        if not self.executables or not all(os.path.isabs(e) for e in self.executables):
            raise ShellConfigError("executables must be absolute candidate paths")

    def locate(self) -> str | None:
        for candidate in self.executables:
            real = os.path.realpath(candidate)
            if os.path.isfile(real) and os.access(real, os.X_OK):
                return candidate
        return None


DEFAULT_ALLOWLIST: Mapping[str, CommandEntry] = {
    entry.name: entry
    for entry in (
        CommandEntry("ls", PermissionLevel.GREEN, ("/bin/ls", "/usr/bin/ls"), _ls),
        CommandEntry("cat", PermissionLevel.YELLOW, ("/bin/cat", "/usr/bin/cat"), _cat),
        CommandEntry(
            "git",
            PermissionLevel.YELLOW,
            ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"),
            _git,
        ),
        # Runs project code; only available when named in JARVIS_SHELL_COMMANDS.
        CommandEntry("pytest", PermissionLevel.YELLOW, (sys.executable,), _pytest),
    )
}


# Status ---------------------------------------------------------------------------------------


class ShellStatusKind(StrEnum):
    OFF = "OFF"
    OK = "OK"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class ShellStatus:
    status: ShellStatusKind
    code: str
    commands: tuple[str, ...] = ()  # names only, never paths


def validate_root(path: Path | str | None) -> str | None:
    """The resolved execution root, or None when it is not acceptable.

    Rejects: unset, relative, missing, not a directory, '/', the home directory and any parent of
    it (a root that wide would let `cat` and `git` read everything of the user's).
    """
    if path is None:
        return None
    text = str(path)
    if not text.strip() or "\x00" in text or not os.path.isabs(text):
        return None
    real = os.path.realpath(text)
    if not os.path.isdir(real) or real == os.sep:
        return None
    home = os.path.realpath(os.path.expanduser("~"))
    if home == real or home.startswith(real.rstrip(os.sep) + os.sep):
        return None
    return real


def inspect_shell(
    settings: Settings, allowlist: Mapping[str, CommandEntry] = DEFAULT_ALLOWLIST
) -> ShellStatus:
    """Pure, read-only. The same rules `build_shell_wiring` applies."""
    if not settings.shell_enabled:
        return ShellStatus(ShellStatusKind.OFF, NOT_CONFIGURED)
    if settings.shell_root is None:
        return ShellStatus(ShellStatusKind.INCOMPLETE, ROOT_MISSING)
    if validate_root(settings.shell_root) is None:
        return ShellStatus(ShellStatusKind.INCOMPLETE, ROOT_INVALID)
    if os.name != "posix":
        return ShellStatus(ShellStatusKind.INCOMPLETE, UNSUPPORTED)
    if any(name not in allowlist for name in settings.shell_commands):
        return ShellStatus(ShellStatusKind.INCOMPLETE, COMMAND_UNKNOWN)
    found = tuple(n for n in settings.shell_commands if allowlist[n].locate() is not None)
    if not found:
        return ShellStatus(ShellStatusKind.INCOMPLETE, NO_COMMANDS)
    return ShellStatus(ShellStatusKind.OK, READY, found)


# Audit ----------------------------------------------------------------------------------------

_SECRET_VALUE = re.compile(
    r"(sk-|sk_|ghp_|gho_|github_pat_|xox[abp]-|AIza|AKIA|eyJ)[A-Za-z0-9_\-]{6,}"
    r"|bearer\s+\S+|^[A-Za-z0-9+/=_\-]{32,}$",
    re.IGNORECASE,
)
_SECRET_FLAG = re.compile(r"pass|secret|token|key|auth|cred|cookie", re.IGNORECASE)


def _clean(text: str) -> str:
    return "".join("?" if unicodedata.category(c)[0] == "C" or c in "  " else c for c in text)


def summarize_argv(arguments: object) -> str:
    """Short, redacted, control-free description of a request. Never raises."""
    try:
        if not isinstance(arguments, Mapping):
            return "<invalid>"
        command = arguments.get("command")
        args = arguments.get("args", ())
        cwd = arguments.get("cwd")
        shown: list[str] = []
        redact_next = False
        items = list(args) if isinstance(args, list | tuple) else []
        for item in items[:MAX_SUMMARY_ARGS]:
            text = _clean(str(item))
            if redact_next or _SECRET_VALUE.search(text.strip()):
                shown.append(REDACTED)
                redact_next = False
                continue
            if text.startswith("-") and _SECRET_FLAG.search(text):
                redact_next = "=" not in text
                shown.append(text.partition("=")[0] + ("=" + REDACTED if "=" in text else ""))
                continue
            shown.append(
                text
                if len(text) <= MAX_SUMMARY_ARG_CHARS
                else text[: MAX_SUMMARY_ARG_CHARS - 1] + "…"
            )
        extra = len(items) - len(shown)
        where = ""
        if isinstance(cwd, Mapping):
            rel = _clean(str(cwd.get("path", "")))[:MAX_SUMMARY_ARG_CHARS]
            where = f" @{_clean(str(cwd.get('root', '')))[:32]}/{rel}"
        name = _clean(str(command))[:32]
        return (name + " " + " ".join(shown) + (f" (+{extra} more)" if extra > 0 else "") + where)[
            :400
        ]
    except Exception:
        return "<unsummarizable>"


@dataclass(frozen=True)
class ShellRunAudit:
    """One shell run or refusal. No output content, no resolved paths."""

    timestamp: datetime
    call_id: str
    tool_name: str
    command: str
    argv_summary: str
    permission: PermissionLevel | None
    confirmed: bool
    outcome: ShellOutcome
    reason: ShellRejection | None
    exit_code: int | None
    signal: int | None
    duration_ms: float
    truncated: bool

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "ts": self.timestamp.isoformat(),
            "call_id": self.call_id,
            "tool": self.tool_name,
            "command": self.command,
            "argv": self.argv_summary,
            "permission": self.permission.value if self.permission else None,
            "confirmed": self.confirmed,
            "outcome": self.outcome.value,
            "reason": self.reason.value if self.reason else None,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }


class ShellRunSink(Protocol):
    def record(self, record: ShellRunAudit) -> None: ...


class LoggingShellRunSink:
    """Default sink: one JSON line on the `jarvis.shell.audit` logger."""

    def record(self, record: ShellRunAudit) -> None:
        audit_logger.info(json.dumps(record.as_log_fields(), ensure_ascii=False, sort_keys=True))


class InMemoryShellRunSink:
    def __init__(self, max_records: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._records: deque[ShellRunAudit] = deque(maxlen=max_records)

    def record(self, record: ShellRunAudit) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> tuple[ShellRunAudit, ...]:
        with self._lock:
            return tuple(self._records)


class _Journal:
    """Joins the argv summary (known when a run starts) to the tool's audit record (its end)."""

    def __init__(self, sink: ShellRunSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._summaries: dict[str, str] = {}

    def begin(self, call_id: str, summary: str) -> None:
        with self._lock:
            self._summaries[call_id] = summary

    def end(self, call_id: str) -> None:
        with self._lock:
            self._summaries.pop(call_id, None)

    def record(self, record: ShellAuditRecord) -> None:
        with self._lock:
            summary = self._summaries.get(record.call_id, "<unknown>")
        self._sink.record(
            ShellRunAudit(
                timestamp=record.timestamp,
                call_id=record.call_id,
                tool_name=record.tool_name,
                command=record.command,
                argv_summary=summary,
                permission=record.permission,
                confirmed=record.confirmed,
                outcome=record.outcome,
                reason=record.reason,
                exit_code=record.exit_code,
                signal=record.signal,
                duration_ms=record.duration_ms,
                truncated=record.truncated,
            )
        )


class AuditedShellTool:
    """Wraps a `ShellTool` so each run is audited with a redacted argv summary."""

    def __init__(self, inner: ShellTool, journal: _Journal) -> None:
        self._inner = inner
        self._journal = journal
        self.spec: ToolSpec = inner.spec

    def scope_check(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> bool:
        return self._inner.scope_check(spec, arguments)

    async def run(self, arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        self._journal.begin(context.call_id, summarize_argv(arguments))
        try:
            return await self._inner.run(arguments, context)
        finally:
            self._journal.end(context.call_id)


# Factory --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ShellWiring:
    registry: ToolRegistry
    executor: ConfirmedExecutor
    commands: tuple[str, ...]
    tool_names: tuple[str, ...]


def build_shell_wiring(
    settings: Settings,
    *,
    approvals: ApprovalRequester,
    run_sink: ShellRunSink | None = None,
    tool_audit: ToolAuditSink | None = None,
    base_policy: PermissionPolicy | None = None,
    allowlist: Mapping[str, CommandEntry] = DEFAULT_ALLOWLIST,
    cancel_grace_seconds: float | None = None,
) -> ShellWiring | None:
    """Register shell tools, or return None when they must not exist.

    None means: the shell is disabled, or the root / commands are not usable (the reason is only
    logged as a fixed code; `inspect_shell` gives the same answer for the doctor). Nothing is
    ever registered by default. Raises `ShellConfigError` only for a programming error in the
    table or `base_policy` (for example auto-allowing Yellow shell calls).
    """
    status = inspect_shell(settings, allowlist)
    if status.status is not ShellStatusKind.OK:
        if status.status is ShellStatusKind.INCOMPLETE:
            logger.warning("shell tools not registered: %s", status.code)
        return None
    root = validate_root(settings.shell_root)
    if root is None:  # inspect_shell already agreed; keep the check next to its use
        return None
    roots = {ROOT_LABEL: root}
    commands: list[AllowedCommand] = []
    try:
        for name in status.commands:
            entry = allowlist[name]
            executable = entry.locate()
            if executable is None:
                continue
            command = entry.build(
                executable, roots, settings.shell_timeout_seconds, settings.shell_max_output_bytes
            )
            if command.name != entry.name or command.permission is not entry.level:
                raise ShellConfigError("allowlist entry and built command disagree")
            commands.append(command)
        if not commands:
            return None
        sink: ShellRunSink = run_sink if run_sink is not None else LoggingShellRunSink()
        journal = _Journal(sink)
        inner = build_shell_tools(CommandCatalog(commands), audit=journal)
    except ShellUnsupportedPlatformError:
        logger.warning("shell tools not registered: %s", UNSUPPORTED)
        return None
    tools = tuple(AuditedShellTool(tool, journal) for tool in inner)
    names = {tool.spec.name for tool in tools}
    base = base_policy if base_policy is not None else PermissionPolicy()
    if names & base.allow_yellow:
        raise ShellConfigError("shell tools cannot be auto-allowed; Yellow needs a human")
    checks = dict(base.scope_checks)
    checks.update(shell_scope_checks(tools))  # type: ignore[arg-type]
    policy = PermissionPolicy(
        deny=base.deny, allow_yellow=base.allow_yellow, scope_checks=checks, clock=base.clock
    )
    kwargs: dict[str, Any] = {"audit": tool_audit}
    if cancel_grace_seconds is not None:
        kwargs["cancel_grace_seconds"] = cancel_grace_seconds
    registry = ToolRegistry(policy, **kwargs)
    for tool in tools:
        registry.register(tool)
    return ShellWiring(
        registry=registry,
        executor=ConfirmedExecutor(registry, approvals),
        commands=tuple(c.name for c in commands),
        tool_names=tuple(sorted(names)),
    )
