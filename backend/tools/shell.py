"""Structured shell tool: named, application-registered commands, never free-form shell.

The model never supplies an executable. It names an `AllowedCommand` that application code
registered, plus arguments and a working directory that are validated against that entry's
declarative, deny-by-default argument grammar. Execution uses argv (no shell, no `sh -c`), a closed
stdin, a scrubbed environment, a new session so the whole process group can be stopped, a
wall-clock timeout, and a hard output cap. The result is inert, untrusted DATA: facts about the
run (exit status, signal, timeout, truncation, captured text), never a verdict. A zero exit code
does not mean the task succeeded; callers judge postconditions with a `ResultVerifier`.

A command allow-list is only as safe as its arguments: many programs run other programs through a
flag (`git -c`, `--upload-pack`, `pytest -p`). The grammar therefore allows nothing by default and
supports explicit forbidden-flag patterns on top.

POSIX only. Constructing an `AllowedCommand` or a `ShellTool` on another platform fails closed.
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import signal
import stat
import tempfile
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from backend.tools.contract import (
    PermissionLevel,
    ToolContext,
    ToolResult,
    ToolSpec,
    ToolStatus,
    canonical_json,
)
from backend.tools.permission import ScopeCheck

logger = logging.getLogger(__name__)

SAFE_PATH = "/usr/bin:/bin"
MAX_OUTPUT_BYTES = 65_536  # combined stdout+stderr; keeps each string inside the default schema
MAX_COMMAND_TIMEOUT_SECONDS = 600.0
MAX_ARGS = 64
MAX_ARG_LENGTH = 1024
MAX_CWD_PATH_LENGTH = 512
MAX_ENV_VALUE_LENGTH = 4096
MAX_FIXED_ARGS = 32
SPEC_TIMEOUT_MARGIN_SECONDS = 15.0  # registry timeout sits above every command's own timeout
DEFAULT_TERM_GRACE_SECONDS = 0.5
MAX_TERM_GRACE_SECONDS = 5.0
DRAIN_SECONDS = 1.0
CLEANUP_CONFIRM_SECONDS = 2.0
MAX_SUBCOMMAND_DEPTH = 3

READONLY_TOOL_NAME = "shell.run_readonly"
RUN_TOOL_NAME = "shell.run"
CONFIRMED_TOOL_NAME = "shell.run_confirmed"
_TOOL_NAMES = {
    PermissionLevel.GREEN: READONLY_TOOL_NAME,
    PermissionLevel.YELLOW: RUN_TOOL_NAME,
    PermissionLevel.RED: CONFIRMED_TOOL_NAME,
}

UNKNOWN_LABEL = "<unknown>"

_COMMAND_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ROOT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_SUBCOMMAND_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Environment names that are never inherited from the parent process.
_SENSITIVE_ENV_RE = re.compile(
    r"KEY|TOKEN|SECRET|PASSW|PASSPHRASE|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|BEARER|CERT",
    re.IGNORECASE,
)
_RISKY_ENV_PREFIXES = (
    "LD_",
    "DYLD_",
    "PYTHON",
    "PERL",
    "RUBY",
    "NODE_",
    "NPM_",
    "PIP_",
    "GIT_",
    "BASH_",
    "JAVA",
    "_JAVA",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCP_",
    "OPENAI_",
    "ANTHROPIC_",
    "GITHUB_",
    "GH_",
    "SSH_",
    "GPG_",
    "KUBE",
    "DOCKER_",
    "XDG_",
    "LC_",
)
_RISKY_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "IFS",
        "ENV",
        "SHELL",
        "SHELLOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "CDPATH",
        "GLOBIGNORE",
        "OLDPWD",
        "PWD",
        "CLASSPATH",
        "TERMINFO",
        "TERMCAP",
        "LANG",
        "LANGUAGE",
    }
)
_FORCED_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)


# Errors ---------------------------------------------------------------------------------------


class ShellRejection(StrEnum):
    """Fixed reason vocabulary. Arguments, paths, and OS error text are never carried."""

    INVALID_REQUEST = "invalid_request"
    UNKNOWN_COMMAND = "unknown_command"
    PERMISSION_MISMATCH = "permission_mismatch"
    CONFIRMATION_MISSING = "confirmation_missing"
    UNKNOWN_CWD_ROOT = "unknown_cwd_root"
    CWD_INVALID_PATH = "cwd_invalid_path"
    CWD_ESCAPES_ROOT = "cwd_escapes_root"
    CWD_NOT_FOUND = "cwd_not_found"
    CWD_NOT_DIRECTORY = "cwd_not_directory"
    CWD_ROOT_CHANGED = "cwd_root_changed"
    ARG_COUNT = "arg_count"
    ARG_LENGTH = "arg_length"
    ARG_CHARACTERS = "arg_characters"
    ARG_NOT_ALLOWED = "arg_not_allowed"
    ARG_FORBIDDEN = "arg_forbidden"
    ARG_VALUE_INVALID = "arg_value_invalid"
    ARG_MISSING_VALUE = "arg_missing_value"
    ARG_REPEATED = "arg_repeated"
    ARG_DOUBLE_DASH = "arg_double_dash"
    ARG_TOO_MANY_POSITIONALS = "arg_too_many_positionals"
    ARG_SUBCOMMAND = "arg_subcommand"
    EXECUTABLE_MISSING = "executable_missing"
    EXECUTABLE_CHANGED = "executable_changed"
    SPAWN_FAILED = "spawn_failed"


class ShellRejected(Exception):
    """A request was refused before (or instead of) running. `str()` is the reason code only."""

    def __init__(self, reason: ShellRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


class ShellConfigError(ValueError):
    """An AllowedCommand, policy, or tool was configured unsafely. Messages never carry paths."""


class ShellUnsupportedPlatformError(ShellConfigError):
    """The shell tool only builds on POSIX systems."""


def _posix_supported() -> bool:
    return os.name == "posix"


def _require_posix() -> None:
    if not _posix_supported():
        raise ShellUnsupportedPlatformError("the shell tool requires a POSIX platform")


# Text safety ----------------------------------------------------------------------------------


def has_unsafe_characters(text: str) -> bool:
    """True for control, format, surrogate, private-use, unassigned, and line-separator chars."""
    if text.isascii():
        return any(c < " " or c == "\x7f" for c in text)
    return any(unicodedata.category(c) in _BAD_CATEGORIES for c in text)


def _neutral_char(char: str) -> str:
    if char in "\n\t":
        return char
    code = ord(char)
    if code < 32:
        return chr(0x2400 + code)  # visible "control picture" instead of the control byte
    if code == 127:
        return "␡"
    if code >= 128 and unicodedata.category(char) in _BAD_CATEGORIES:
        return "�"
    return char


def sanitize_output(data: bytes) -> str:
    """Decode child output as inert text: invalid UTF-8 and control characters are replaced.

    Never produces more characters than input bytes, so the byte cap bounds the text size.
    """
    return "".join(_neutral_char(c) for c in data.decode("utf-8", errors="replace"))


# Argument grammar -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgContext:
    """Where an argument is being validated: the resolved cwd and whether `--` was seen."""

    cwd: str | None
    after_double_dash: bool = False


class ArgValidator(Protocol):
    def accepts(self, value: str, context: ArgContext) -> bool: ...


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class OneOf:
    """Exact string match against a fixed set."""

    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.values or not all(isinstance(v, str) and v for v in self.values):
            raise ShellConfigError("OneOf needs one or more non-empty strings")
        object.__setattr__(self, "values", tuple(self.values))

    def accepts(self, value: str, context: ArgContext) -> bool:
        return value in self.values


@dataclass(frozen=True)
class Matches:
    """Anchored regular expression (the whole value must match). Use explicit ASCII classes."""

    pattern: str
    allow_leading_dash: bool = False
    _regex: "re.Pattern[str]" = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not 0 < len(self.pattern) <= 300:
            raise ShellConfigError("Matches needs a pattern of 1-300 characters")
        try:
            object.__setattr__(self, "_regex", re.compile(self.pattern, re.ASCII))
        except re.error as exc:
            raise ShellConfigError("Matches pattern does not compile") from exc

    def accepts(self, value: str, context: ArgContext) -> bool:
        if value.startswith("-") and not (self.allow_leading_dash or context.after_double_dash):
            return False
        return self._regex.fullmatch(value) is not None


@dataclass(frozen=True)
class IntRange:
    """Plain decimal digits within [minimum, maximum]."""

    minimum: int = 0
    maximum: int = 1_000_000

    def __post_init__(self) -> None:
        if not 0 <= self.minimum <= self.maximum <= 10**9:
            raise ShellConfigError("IntRange needs 0 <= minimum <= maximum <= 1e9")

    def accepts(self, value: str, context: ArgContext) -> bool:
        if re.fullmatch(r"[0-9]{1,10}", value) is None:
            return False
        return self.minimum <= int(value) <= self.maximum


@dataclass(frozen=True)
class PlainText:
    """Free text without control characters; shell metacharacters are inert but unvalidated.

    Free-form validators make a policy not `is_closed()`, so it cannot be Green.
    """

    max_length: int = 128
    allow_leading_dash: bool = False

    def __post_init__(self) -> None:
        if not 0 < self.max_length <= MAX_ARG_LENGTH:
            raise ShellConfigError("PlainText max_length must be in 1..1024")

    def accepts(self, value: str, context: ArgContext) -> bool:
        if value.startswith("-") and not (self.allow_leading_dash or context.after_double_dash):
            return False
        return 0 < len(value) <= self.max_length and not has_unsafe_characters(value)


@dataclass(frozen=True)
class RelPath:
    """A relative path that stays inside the cwd after symlink resolution.

    Rejects absolute paths, any `..` segment, a leading `-` (before `--`), and symlinks that
    leave the cwd. The path need not exist. Resolution happens at validation time; a symlink
    swapped afterwards is a residual race documented in docs/tools-shell.md.
    """

    def accepts(self, value: str, context: ArgContext) -> bool:
        if context.cwd is None or value.startswith("/") or "\x00" in value:
            return False
        if value.startswith("-") and not context.after_double_dash:
            return False
        if ".." in value.split("/"):
            return False
        return _inside(os.path.realpath(os.path.join(context.cwd, value)), context.cwd)


class ValueStyle(StrEnum):
    SEPARATE = "separate"  # `--flag value`
    JOINED = "joined"  # `--flag=value`
    EITHER = "either"


@dataclass(frozen=True)
class FlagRule:
    """One allowed flag: an exact string or an anchored regex, optionally taking a value."""

    name: str | None = None
    pattern: str | None = None
    value: ArgValidator | None = None
    style: ValueStyle = ValueStyle.SEPARATE
    max_uses: int = 1
    _regex: "re.Pattern[str] | None" = field(
        default=None, init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if (self.name is None) == (self.pattern is None):
            raise ShellConfigError("a flag rule needs exactly one of name or pattern")
        if self.name is not None and (
            not isinstance(self.name, str)
            or not self.name.startswith("-")
            or self.name == "--"
            or "=" in self.name
            or has_unsafe_characters(self.name)
            or len(self.name) > 64
        ):
            raise ShellConfigError("flag names start with '-', and hold no '=' or controls")
        if self.pattern is not None:
            try:
                regex = re.compile(self.pattern, re.ASCII)
            except re.error as exc:
                raise ShellConfigError("flag pattern does not compile") from exc
            if not self.pattern.startswith("-"):
                raise ShellConfigError("flag patterns must start with '-'")
            object.__setattr__(self, "_regex", regex)
        if not isinstance(self.style, ValueStyle):
            raise ShellConfigError("style must be a ValueStyle")
        if self.value is not None and not callable(getattr(self.value, "accepts", None)):
            raise ShellConfigError("flag value must be an ArgValidator")
        if not 1 <= self.max_uses <= MAX_ARGS:
            raise ShellConfigError("max_uses must be in 1..64")

    def _head_matches(self, head: str) -> bool:
        if self.name is not None:
            return head == self.name
        return self._regex is not None and self._regex.fullmatch(head) is not None

    def match(self, token: str) -> tuple[bool, str | None]:
        """(matched, inline_value). inline_value is set for `--flag=value` forms."""
        if self.value is None:
            return self._head_matches(token), None
        if self.style is not ValueStyle.SEPARATE and "=" in token:
            head, _, inline = token.partition("=")
            if self._head_matches(head):
                return True, inline
        if self.style is not ValueStyle.JOINED and self._head_matches(token):
            return True, None
        return False, None


@dataclass(frozen=True)
class ArgumentPolicy:
    """Declarative, deny-by-default grammar for one command (or one subcommand).

    - `subcommands`: when set, the first argument must be exactly one of the keys and the rest
      is checked by that nested policy (root flags/positionals must then be empty).
    - `flags`: every token starting with `-` must match a rule; otherwise it is rejected.
    - `positionals`/`max_positionals`: values not starting with `-` (before `--`).
    - `after_double_dash`/`max_after_double_dash`: `--` is rejected unless this validator is set.
    - `forbidden`: anchored regexes for flags known to run other programs; checked first on every
      flag-looking token (whole token and the part before `=`), with a distinct reason code.
    - `inject_args`: trusted tokens inserted right after the subcommand (or at the start).
    """

    flags: tuple[FlagRule, ...] = ()
    positionals: ArgValidator | None = field(default=None, compare=False, hash=False)
    max_positionals: int = 0
    after_double_dash: ArgValidator | None = field(default=None, compare=False, hash=False)
    max_after_double_dash: int = 0
    subcommands: Mapping[str, "ArgumentPolicy"] = field(
        default_factory=dict, compare=False, hash=False
    )
    forbidden: tuple[str, ...] = ()
    inject_args: tuple[str, ...] = ()
    max_args: int = 32
    max_arg_length: int = 256
    _forbidden_re: tuple["re.Pattern[str]", ...] = field(
        default=(), init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "flags", tuple(self.flags))
        object.__setattr__(self, "forbidden", tuple(self.forbidden))
        object.__setattr__(self, "inject_args", tuple(self.inject_args))
        if not all(isinstance(rule, FlagRule) for rule in self.flags):
            raise ShellConfigError("flags must be FlagRule entries")
        if not 0 < self.max_args <= MAX_ARGS or not 0 < self.max_arg_length <= MAX_ARG_LENGTH:
            raise ShellConfigError("max_args must be in 1..64 and max_arg_length in 1..1024")
        for validator, count, label in (
            (self.positionals, self.max_positionals, "positionals"),
            (self.after_double_dash, self.max_after_double_dash, "after_double_dash"),
        ):
            if (validator is None) != (count == 0) or not 0 <= count <= MAX_ARGS:
                raise ShellConfigError(
                    f"{label} needs a validator and a count in 1..64, or neither"
                )
        if not all(
            isinstance(t, str) and "\x00" not in t and len(t) <= MAX_ARG_LENGTH
            for t in self.inject_args
        ):
            raise ShellConfigError("inject_args must be short strings without NUL")
        subs = dict(self.subcommands)
        object.__setattr__(self, "subcommands", MappingProxyType(subs))
        if subs:
            if self.flags or self.positionals is not None or self.after_double_dash is not None:
                raise ShellConfigError("a policy with subcommands takes no flags or positionals")
            for key, nested in subs.items():
                if not isinstance(key, str) or _SUBCOMMAND_RE.fullmatch(key) is None:
                    raise ShellConfigError("subcommand names are short alphanumeric words")
                if not isinstance(nested, ArgumentPolicy):
                    raise ShellConfigError("subcommands map to ArgumentPolicy")
        compiled = []
        for pattern in self.forbidden:
            try:
                compiled.append(re.compile(pattern, re.ASCII))
            except (re.error, TypeError) as exc:
                raise ShellConfigError("forbidden pattern does not compile") from exc
        object.__setattr__(self, "_forbidden_re", tuple(compiled))
        for name in self._flag_names():
            if any(p.fullmatch(name) for p in compiled):
                raise ShellConfigError("an allowed flag matches a forbidden pattern")
        if self._depth() > MAX_SUBCOMMAND_DEPTH:
            raise ShellConfigError("subcommands nested too deeply")

    def _flag_names(self) -> Iterable[str]:
        for rule in self.flags:
            if rule.name is not None:
                yield rule.name
        for nested in self.subcommands.values():
            yield from nested._flag_names()

    def _depth(self) -> int:
        return 1 + max((s._depth() for s in self.subcommands.values()), default=0)

    def is_closed(self) -> bool:
        """True when every argument is matched by an exact string, regex, or bounded integer.

        Free-form positionals, relative paths, and plain text make a policy open. Only closed
        policies may back a Green (read_only) command.
        """
        if self.positionals is not None or self.after_double_dash is not None:
            return False
        for rule in self.flags:
            if rule.value is not None and not isinstance(rule.value, OneOf | Matches | IntRange):
                return False
        return all(sub.is_closed() for sub in self.subcommands.values())

    def parse(self, args: object, cwd: str | None) -> tuple[str, ...]:
        """Validate `args` and return the argv tail (with injected tokens); raise ShellRejected."""
        if not isinstance(args, list | tuple):
            raise ShellRejected(ShellRejection.INVALID_REQUEST)
        tokens = list(args)
        out: list[str] = list(self.inject_args)
        policy = self
        forbidden = list(self._forbidden_re)  # a parent's forbidden flags apply to subcommands
        while True:
            policy._check_tokens(tokens)
            if not policy.subcommands:
                break
            if not tokens or tokens[0] not in policy.subcommands:
                raise ShellRejected(ShellRejection.ARG_SUBCOMMAND)
            head = tokens[0]
            policy = policy.subcommands[head]
            forbidden.extend(policy._forbidden_re)
            tokens = tokens[1:]
            out.append(head)
            out.extend(policy.inject_args)
        policy._parse_flags(tokens, cwd, out, forbidden)
        return tuple(out)

    def _check_tokens(self, tokens: Sequence[object]) -> None:
        if len(tokens) > self.max_args:
            raise ShellRejected(ShellRejection.ARG_COUNT)
        for token in tokens:
            if not isinstance(token, str) or not 0 < len(token) <= self.max_arg_length:
                raise ShellRejected(ShellRejection.ARG_LENGTH)
            if has_unsafe_characters(token):
                raise ShellRejected(ShellRejection.ARG_CHARACTERS)

    def _parse_flags(
        self,
        tokens: Sequence[str],
        cwd: str | None,
        out: list[str],
        forbidden: Sequence["re.Pattern[str]"],
    ) -> None:
        uses: dict[int, int] = {}
        positionals = after = 0
        after_dd = False
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if after_dd:
                validator = self.after_double_dash
                if validator is None or after >= self.max_after_double_dash:
                    raise ShellRejected(ShellRejection.ARG_TOO_MANY_POSITIONALS)
                if not _accepts(validator, token, ArgContext(cwd, True)):
                    raise ShellRejected(ShellRejection.ARG_VALUE_INVALID)
                after += 1
                out.append(token)
                i += 1
            elif token == "--":
                if self.after_double_dash is None:
                    raise ShellRejected(ShellRejection.ARG_DOUBLE_DASH)
                after_dd = True
                out.append(token)
                i += 1
            elif token.startswith("-"):
                i = self._take_flag(tokens, i, cwd, uses, out, forbidden)
            else:
                validator = self.positionals
                if validator is None or positionals >= self.max_positionals:
                    raise ShellRejected(ShellRejection.ARG_TOO_MANY_POSITIONALS)
                if not _accepts(validator, token, ArgContext(cwd, False)):
                    raise ShellRejected(ShellRejection.ARG_VALUE_INVALID)
                positionals += 1
                out.append(token)
                i += 1

    def _take_flag(
        self,
        tokens: Sequence[str],
        i: int,
        cwd: str | None,
        uses: dict[int, int],
        out: list[str],
        forbidden: Sequence["re.Pattern[str]"],
    ) -> int:
        token = tokens[i]
        head = token.partition("=")[0]
        if any(p.fullmatch(token) or p.fullmatch(head) for p in forbidden):
            raise ShellRejected(ShellRejection.ARG_FORBIDDEN)
        for index, rule in enumerate(self.flags):
            matched, inline = rule.match(token)
            if not matched:
                continue
            uses[index] = uses.get(index, 0) + 1
            if uses[index] > rule.max_uses:
                raise ShellRejected(ShellRejection.ARG_REPEATED)
            out.append(token)
            if rule.value is None:
                return i + 1
            if inline is not None:
                value, step = inline, 1
            else:
                if i + 1 >= len(tokens):
                    raise ShellRejected(ShellRejection.ARG_MISSING_VALUE)
                value, step = tokens[i + 1], 2
                out.append(value)
            if not _accepts(rule.value, value, ArgContext(cwd, False)):
                raise ShellRejected(ShellRejection.ARG_VALUE_INVALID)
            return i + step
        raise ShellRejected(ShellRejection.ARG_NOT_ALLOWED)


def _accepts(validator: ArgValidator, value: str, context: ArgContext) -> bool:
    try:
        return validator.accepts(value, context) is True
    except Exception:
        return False


# Allowed commands -----------------------------------------------------------------------------


def _resolve_executable(path: object) -> tuple[str, tuple[int, int]]:
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise ShellConfigError("executable must be an absolute path")
    real = os.path.realpath(path)
    try:
        info = os.stat(real)
    except OSError as exc:
        raise ShellConfigError("executable does not exist") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
        raise ShellConfigError("executable must be an executable regular file")
    return real, (info.st_dev, info.st_ino)


def _resolve_root(path: object) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise ShellConfigError("cwd roots must be absolute paths")
    real = os.path.realpath(path)
    if real == os.sep or not os.path.isdir(real):
        raise ShellConfigError("cwd roots must be existing directories other than '/'")
    return real


def _check_env_name(name: object, *, inherit: bool) -> str:
    if not isinstance(name, str) or _ENV_NAME_RE.fullmatch(name) is None:
        raise ShellConfigError("environment variable names are [A-Za-z_][A-Za-z0-9_]*")
    if _SENSITIVE_ENV_RE.search(name) or name in _FORCED_ENV_NAMES:
        raise ShellConfigError("environment variable name is sensitive or reserved")
    if inherit and (name in _RISKY_ENV_NAMES or name.startswith(_RISKY_ENV_PREFIXES)):
        raise ShellConfigError("environment variable cannot be inherited from the parent")
    return name


@dataclass(frozen=True)
class AllowedCommand:
    """An application-registered command. The model can only name it.

    `executable` must be an absolute path to an existing executable file; it is resolved with
    realpath at registration and executed by that resolved path (a venv interpreter therefore
    resolves to its base interpreter). `permission` is Yellow or Red; Green needs
    `read_only=True` and a closed argument policy. `env_allowlist` lists parent variables that are
    inherited; everything else is dropped, PATH is fixed, and HOME/TMPDIR/XDG_* point at a
    per-run temporary directory. `env_fixed` sets constants chosen by the application.
    `fixed_args` are trusted tokens placed before the validated ones.
    """

    name: str
    executable: str
    permission: PermissionLevel
    argument_policy: ArgumentPolicy = field(compare=False, hash=False)
    cwd_roots: Mapping[str, str] = field(compare=False, hash=False)
    env_allowlist: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    max_output_bytes: int = 32_768
    cancellable: bool = True
    read_only: bool = False
    fixed_args: tuple[str, ...] = ()
    env_fixed: Mapping[str, str] = field(default_factory=dict, compare=False, hash=False)
    resolved_executable: str = field(init=False, default="", repr=False)
    identity: tuple[int, int] = field(init=False, default=(0, 0), repr=False)
    root_declared: Mapping[str, str] = field(
        init=False, default_factory=dict, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        _require_posix()
        if not isinstance(self.name, str) or _COMMAND_NAME_RE.fullmatch(self.name) is None:
            raise ShellConfigError("command name must be a short lowercase label")
        if not isinstance(self.permission, PermissionLevel):
            raise ShellConfigError("permission must be a PermissionLevel")
        if not isinstance(self.argument_policy, ArgumentPolicy):
            raise ShellConfigError("argument_policy must be an ArgumentPolicy")
        if not isinstance(self.read_only, bool) or not isinstance(self.cancellable, bool):
            raise ShellConfigError("read_only and cancellable must be booleans")
        if self.permission is PermissionLevel.GREEN:
            if not self.read_only or not self.argument_policy.is_closed():
                raise ShellConfigError("Green needs read_only=True and a closed argument policy")
        elif self.read_only and self.permission is PermissionLevel.RED:
            raise ShellConfigError("a Red command cannot be read_only")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not 0 < self.timeout_seconds <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise ShellConfigError("timeout_seconds must be in (0, 600]")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or not 1 <= self.max_output_bytes <= MAX_OUTPUT_BYTES
        ):
            raise ShellConfigError("max_output_bytes must be in 1..65536")
        real, identity = _resolve_executable(self.executable)
        object.__setattr__(self, "resolved_executable", real)
        object.__setattr__(self, "identity", identity)

        roots = dict(self.cwd_roots)
        if not roots or len(roots) > 16:
            raise ShellConfigError("one to sixteen named cwd roots are required")
        resolved: dict[str, str] = {}
        for key, path in roots.items():
            if not isinstance(key, str) or _ROOT_NAME_RE.fullmatch(key) is None:
                raise ShellConfigError("cwd root names are short lowercase labels")
            resolved[key] = _resolve_root(path)
        object.__setattr__(self, "root_declared", MappingProxyType(dict(roots)))
        object.__setattr__(self, "cwd_roots", MappingProxyType(resolved))

        object.__setattr__(
            self,
            "env_allowlist",
            tuple(_check_env_name(n, inherit=True) for n in self.env_allowlist),
        )
        fixed = {_check_env_name(k, inherit=False): v for k, v in dict(self.env_fixed).items()}
        if not all(
            isinstance(v, str) and "\x00" not in v and len(v) <= MAX_ENV_VALUE_LENGTH
            for v in fixed.values()
        ):
            raise ShellConfigError("fixed environment values must be short strings")
        if set(fixed) & set(self.env_allowlist):
            raise ShellConfigError("an environment name cannot be both inherited and fixed")
        object.__setattr__(self, "env_fixed", MappingProxyType(fixed))
        object.__setattr__(self, "fixed_args", tuple(self.fixed_args))
        if len(self.fixed_args) > MAX_FIXED_ARGS or not all(
            isinstance(a, str) and "\x00" not in a and len(a) <= MAX_ARG_LENGTH
            for a in self.fixed_args
        ):
            raise ShellConfigError("fixed_args must be a few short strings without NUL")


class CommandCatalog:
    """Immutable set of allowed commands, keyed by name."""

    def __init__(self, commands: Iterable[AllowedCommand]) -> None:
        _require_posix()
        entries: dict[str, AllowedCommand] = {}
        for command in commands:
            if not isinstance(command, AllowedCommand):
                raise ShellConfigError("catalog entries must be AllowedCommand")
            if command.name in entries:
                raise ShellConfigError("duplicate command name")
            entries[command.name] = command
        if len(entries) > 64:
            raise ShellConfigError("too many commands")
        self._entries = MappingProxyType(entries)

    @property
    def commands(self) -> tuple[AllowedCommand, ...]:
        return tuple(self._entries.values())

    def get(self, name: str) -> AllowedCommand | None:
        return self._entries.get(name) if isinstance(name, str) else None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))


def build_environment(
    command: AllowedCommand, parent: Mapping[str, str], scratch: str
) -> dict[str, str]:
    """The only environment a child sees: allow-listed names, fixed values, safe PATH, temp HOME."""
    env: dict[str, str] = {}
    for name in command.env_allowlist:
        value = parent.get(name)
        if isinstance(value, str) and len(value) <= MAX_ENV_VALUE_LENGTH and "\x00" not in value:
            env[name] = value
    env.update(command.env_fixed)
    env["PATH"] = SAFE_PATH
    env["HOME"] = scratch
    env["TMPDIR"] = os.path.join(scratch, "tmp")
    env["XDG_CONFIG_HOME"] = os.path.join(scratch, "config")
    env["XDG_CACHE_HOME"] = os.path.join(scratch, "cache")
    env["XDG_DATA_HOME"] = os.path.join(scratch, "data")
    env["XDG_STATE_HOME"] = os.path.join(scratch, "state")
    return env


# Results, audit, verification -----------------------------------------------------------------


class ShellOutcome(StrEnum):
    COMPLETED = "completed"  # exit status 0 (NOT proof that the task succeeded)
    NONZERO_EXIT = "nonzero_exit"
    SIGNALED = "signaled"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_CAP = "output_cap"
    REJECTED = "rejected"
    SPAWN_FAILED = "spawn_failed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ShellAuditRecord:
    """One shell run or refusal. Digests, counts, and sizes only: no argument values, no output."""

    timestamp: datetime
    call_id: str
    tool_name: str
    command: str
    permission: PermissionLevel | None
    outcome: ShellOutcome
    reason: ShellRejection | None
    confirmed: bool
    exit_code: int | None
    signal: int | None
    argument_digest: str | None
    argument_count: int | None
    cwd_root: str | None
    cwd_digest: str | None
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool
    cleanup_complete: bool | None
    duration_ms: float


class ShellAuditSink(Protocol):
    def record(self, record: ShellAuditRecord) -> None: ...


class InMemoryShellAuditSink:
    def __init__(self, max_records: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._records: deque[ShellAuditRecord] = deque(maxlen=max_records)

    def record(self, record: ShellAuditRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> tuple[ShellAuditRecord, ...]:
        with self._lock:
            return tuple(self._records)


@dataclass(frozen=True)
class ShellFacts:
    """Typed view of a `shell.run` output. Facts only; `exit_zero` is not success."""

    command: str
    exit_code: int
    signal: int
    timed_out: bool
    cancelled: bool
    truncated: bool
    stdout: str
    stderr: str
    duration_ms: float
    cleanup_complete: bool

    @classmethod
    def from_output(cls, output: Mapping[str, Any]) -> "ShellFacts":
        return cls(
            command=str(output["command"]),
            exit_code=int(output["exit_code"]),
            signal=int(output["signal"]),
            timed_out=bool(output["timed_out"]),
            cancelled=bool(output["cancelled"]),
            truncated=bool(output["truncated"]),
            stdout=str(output["stdout"]),
            stderr=str(output["stderr"]),
            duration_ms=float(output["duration_ms"]),
            cleanup_complete=bool(output["cleanup_complete"]),
        )

    @property
    def exit_zero(self) -> bool:
        """Process ended with status 0, nothing was cut short, and no process was left behind.

        Not a verdict on the task."""
        return (
            self.exit_code == 0
            and self.signal == 0
            and self.cleanup_complete
            and not (self.timed_out or self.cancelled or self.truncated)
        )


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"  # nobody judged the postcondition


_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class VerificationOutcome:
    status: VerificationStatus
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, VerificationStatus):
            raise ValueError("status must be a VerificationStatus")
        if not isinstance(self.reason_code, str) or _REASON_RE.fullmatch(self.reason_code) is None:
            raise ValueError("reason_code must be a short lowercase code")

    @classmethod
    def passed(cls, reason_code: str = "postcondition_met") -> "VerificationOutcome":
        return cls(VerificationStatus.VERIFIED, reason_code)

    @classmethod
    def failed(cls, reason_code: str = "postcondition_not_met") -> "VerificationOutcome":
        return cls(VerificationStatus.FAILED, reason_code)


class ResultVerifier(Protocol):
    """Caller-supplied judge of the real postcondition (files, repository state, test report...).

    It receives the run facts and returns a `VerificationOutcome`. The tool never calls it.
    """

    def __call__(self, facts: ShellFacts) -> VerificationOutcome: ...


def verify_shell_result(result: ToolResult, verifier: ResultVerifier | None) -> VerificationOutcome:
    """Combine a tool result with an optional verifier. Without a verifier the answer is
    UNVERIFIED even for exit status 0, and a non-ok tool result can never verify."""
    if result.status is not ToolStatus.OK or result.output is None:
        return VerificationOutcome.failed("tool_not_ok")
    try:
        facts = ShellFacts.from_output(result.output)
    except (KeyError, TypeError, ValueError):
        return VerificationOutcome.failed("malformed_output")
    if verifier is None:
        return VerificationOutcome(VerificationStatus.UNVERIFIED, "no_verifier")
    try:
        outcome = verifier(facts)
    except Exception:
        return VerificationOutcome.failed("verifier_error")
    if not isinstance(outcome, VerificationOutcome):
        return VerificationOutcome.failed("verifier_invalid")
    return outcome


# The tool -------------------------------------------------------------------------------------


def _string_schema(max_length: int, min_length: int = 0) -> dict[str, Any]:
    return {"type": "string", "minLength": min_length, "maxLength": max_length}


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": _string_schema(32, 1),
        "args": {
            "type": "array",
            "items": _string_schema(MAX_ARG_LENGTH),
            "maxItems": MAX_ARGS,
        },
        "cwd": {
            "type": "object",
            "properties": {
                "root": _string_schema(32, 1),
                "path": _string_schema(MAX_CWD_PATH_LENGTH),
            },
            "required": ["root"],
        },
    },
    "required": ["command", "cwd"],
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "exit_code": {"type": "integer"},  # -1 when the process did not exit by itself
        "signal": {"type": "integer"},  # 0 when not killed by a signal
        "signal_name": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "cancelled": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "stdout": {"type": "string", "maxLength": MAX_OUTPUT_BYTES},
        "stderr": {"type": "string", "maxLength": MAX_OUTPUT_BYTES},
        "stdout_bytes": {"type": "integer"},
        "stderr_bytes": {"type": "integer"},
        "duration_ms": {"type": "number"},
        "cleanup_complete": {"type": "boolean"},
    },
    "required": [
        "command",
        "exit_code",
        "signal",
        "signal_name",
        "timed_out",
        "cancelled",
        "truncated",
        "stdout",
        "stderr",
        "stdout_bytes",
        "stderr_bytes",
        "duration_ms",
        "cleanup_complete",
    ],
}


@dataclass(frozen=True)
class _Prepared:
    command: AllowedCommand
    argv: tuple[str, ...]
    cwd: str
    cwd_root: str
    argument_digest: str
    argument_count: int
    cwd_digest: str


@dataclass
class _Trace:
    """Mutable facts about one run, so cancellation and failure paths can still be audited."""

    command: str = UNKNOWN_LABEL
    permission: PermissionLevel | None = None
    confirmed: bool = False
    prepared: _Prepared | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    exit_code: int | None = None
    signal: int | None = None
    cleanup_complete: bool | None = None
    timed_out: bool = False
    cancelled: bool = False


class _Budget:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0
        self.truncated = False


async def _pump(stream: asyncio.StreamReader, sink: bytearray, budget: _Budget) -> None:
    """Copy a pipe into `sink` until EOF, keeping at most the shared cap.

    Bytes over the cap are read and discarded so a chatty command is not blocked or killed;
    only the timeout and cancellation stop a run.
    """
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        room = max(budget.cap - budget.used, 0)
        if len(chunk) > room:
            budget.truncated = True
        keep = chunk[:room]
        sink += keep
        budget.used += len(keep)


async def _returncode(proc: "asyncio.subprocess.Process") -> int:
    """Wait for the child itself to exit.

    `Process.wait()` also waits for the pipes to close, which a background grandchild can hold
    open, so poll the exit status that asyncio records as soon as the child is reaped.
    """
    delay = 0.005
    while proc.returncode is None:
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 0.1)  # quick for short commands, cheap for long ones
    return proc.returncode


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _confirm_group_gone(pgid: int, limit: float = CLEANUP_CONFIRM_SECONDS) -> bool:
    deadline = time.monotonic() + limit
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return ""


class ShellTool:
    """Runs registered commands of one permission level. Implements the `Tool` protocol.

    Tool names by level: Green `shell.run_readonly`, Yellow `shell.run`, Red
    `shell.run_confirmed`. Every request is re-validated here (second gate) even when
    `shell_scope_checks` already screened it, and Red commands additionally require
    `context.confirmed`, which only the registry sets after accepting a one-time grant.
    """

    def __init__(
        self,
        catalog: CommandCatalog,
        permission: PermissionLevel,
        *,
        audit: ShellAuditSink | None = None,
        parent_environ: Mapping[str, str] | None = None,
        term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS,
    ) -> None:
        _require_posix()
        if not isinstance(permission, PermissionLevel):
            raise ShellConfigError("permission must be a PermissionLevel")
        if not 0 <= term_grace_seconds <= MAX_TERM_GRACE_SECONDS:
            raise ShellConfigError("term_grace_seconds must be in 0..5")
        commands = {c.name: c for c in catalog.commands if c.permission is permission}
        if not commands:
            raise ShellConfigError("no commands are registered at this permission level")
        self._commands = MappingProxyType(commands)
        self._audit = audit
        self._parent_environ = parent_environ
        self._grace = term_grace_seconds
        names = ", ".join(sorted(commands))
        self.spec = ToolSpec(
            name=_TOOL_NAMES[permission],
            description=(
                "Run a pre-registered command by name without a shell. Arguments are checked "
                f"against that command's allow-list. Registered commands: {names}."
            )[:1000],
            input_schema=INPUT_SCHEMA,
            output_schema=OUTPUT_SCHEMA,
            permission=permission,
            environment="local",
            timeout_seconds=max(c.timeout_seconds for c in commands.values())
            + SPEC_TIMEOUT_MARGIN_SECONDS,
            cancellable=all(c.cancellable for c in commands.values()),
            idempotent=False,
        )

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._commands))

    # Validation -------------------------------------------------------------------------------

    def check_request(self, arguments: object) -> None:
        """Raise ShellRejected unless `arguments` is a request this tool would accept."""
        self._prepare(arguments)

    def scope_check(self, spec: ToolSpec, arguments: Mapping[str, Any]) -> bool:
        """PermissionPolicy scope predicate: fail closed on any problem."""
        try:
            self._prepare(arguments)
        except Exception:
            return False
        return True

    def _prepare(self, arguments: object, trace: _Trace | None = None) -> _Prepared:
        if not isinstance(arguments, Mapping) or set(arguments) - {"command", "args", "cwd"}:
            raise ShellRejected(ShellRejection.INVALID_REQUEST)
        name = arguments.get("command")
        if not isinstance(name, str):
            raise ShellRejected(ShellRejection.INVALID_REQUEST)
        command = self._commands.get(name)
        if command is None:
            raise ShellRejected(ShellRejection.UNKNOWN_COMMAND)
        if trace is not None:
            trace.command = command.name
        cwd_root, cwd = _resolve_cwd(command, arguments.get("cwd"))
        args = arguments.get("args", ())
        tail = command.argument_policy.parse(args, cwd)
        _verify_executable(command)
        arg_list = list(args)
        return _Prepared(
            command=command,
            argv=(*command.fixed_args, *tail),
            cwd=cwd,
            cwd_root=cwd_root,
            argument_digest=_digest(arg_list),
            argument_count=len(arg_list),
            cwd_digest=_digest(dict(arguments["cwd"])),
        )

    # Execution --------------------------------------------------------------------------------

    async def run(self, arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        started = time.monotonic()
        trace = _Trace(permission=context.permission, confirmed=context.confirmed)
        try:
            prepared = self._prepare(arguments, trace)
            trace.prepared = prepared
            self._second_gate(prepared.command, context)
            output = await self._execute(prepared, context, trace)
        except ShellRejected as exc:
            logger.warning("shell request refused: %s", exc.reason.value)
            outcome = (
                ShellOutcome.SPAWN_FAILED
                if exc.reason is ShellRejection.SPAWN_FAILED
                else ShellOutcome.REJECTED
            )
            self._record(context, trace, started, outcome, exc.reason)
            raise
        except asyncio.CancelledError:
            trace.cancelled = True
            self._record(context, trace, started, ShellOutcome.CANCELLED, None)
            raise
        except Exception:
            self._record(context, trace, started, ShellOutcome.INTERNAL_ERROR, None)
            raise
        self._record(context, trace, started, _classify(trace), None)
        return output

    def _second_gate(self, command: AllowedCommand, context: ToolContext) -> None:
        if context.tool_name != self.spec.name or context.permission is not command.permission:
            raise ShellRejected(ShellRejection.PERMISSION_MISMATCH)
        if command.permission is PermissionLevel.RED and context.confirmed is not True:
            raise ShellRejected(ShellRejection.CONFIRMATION_MISSING)

    async def _execute(
        self, prepared: _Prepared, context: ToolContext, trace: _Trace
    ) -> dict[str, Any]:
        command = prepared.command
        parent = self._parent_environ if self._parent_environ is not None else os.environ
        scratch = tempfile.mkdtemp(prefix="jarvis-shell-")
        try:
            for sub in ("tmp", "config", "cache", "data", "state"):
                os.mkdir(os.path.join(scratch, sub), 0o700)
            env = build_environment(command, parent, scratch)
            return await self._supervise(prepared, env, context, trace)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    async def _supervise(
        self, prepared: _Prepared, env: dict[str, str], context: ToolContext, trace: _Trace
    ) -> dict[str, Any]:
        command = prepared.command
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                command.resolved_executable,
                *prepared.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=prepared.cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError):
            raise ShellRejected(ShellRejection.SPAWN_FAILED) from None
        pgid = proc.pid
        budget = _Budget(command.max_output_bytes)
        out_buf, err_buf = bytearray(), bytearray()
        if proc.stdout is None or proc.stderr is None:
            _signal_group(pgid, signal.SIGKILL)
            raise ShellRejected(ShellRejection.SPAWN_FAILED)
        pumps = {
            asyncio.ensure_future(_pump(proc.stdout, out_buf, budget)),
            asyncio.ensure_future(_pump(proc.stderr, err_buf, budget)),
        }
        exit_task = asyncio.ensure_future(_returncode(proc))
        cancel_task = (
            asyncio.ensure_future(context.cancellation.wait()) if command.cancellable else None
        )
        helpers = {t for t in (exit_task, cancel_task) if t is not None}
        try:
            done, _ = await asyncio.wait(
                helpers, timeout=command.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if exit_task in done:
                # No background process outlives the run. Skip the signal when the group is
                # already empty so a recycled group id is never signalled.
                if _group_alive(pgid):
                    _signal_group(pgid, signal.SIGKILL)
            else:
                trace.cancelled = cancel_task is not None and cancel_task in done
                trace.timed_out = not trace.cancelled
                _signal_group(pgid, signal.SIGTERM)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(_returncode(proc), self._grace)
                _signal_group(pgid, signal.SIGKILL)
            await asyncio.wait({exit_task}, timeout=CLEANUP_CONFIRM_SECONDS)
            await asyncio.wait(pumps, timeout=DRAIN_SECONDS)
            trace.cleanup_complete = await _confirm_group_gone(pgid)
        except asyncio.CancelledError:
            _signal_group(pgid, signal.SIGKILL)
            trace.cancelled = True
            self._snapshot(trace, out_buf, err_buf, budget)
            with contextlib.suppress(asyncio.CancelledError):
                trace.cleanup_complete = await _confirm_group_gone(pgid, 0.5)
            raise
        finally:
            for task in (*helpers, *pumps):
                if not task.done():
                    task.cancel()
            # Release the pipe transports even when a straggler kept a pipe open or we were
            # cancelled mid-drain. asyncio exposes no public close for a Process.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        self._snapshot(trace, out_buf, err_buf, budget)
        code = proc.returncode
        if code is None:
            trace.exit_code, trace.signal = -1, 0
        elif code < 0:
            trace.exit_code, trace.signal = -1, -code
        else:
            trace.exit_code, trace.signal = code, 0
        return {
            "command": command.name,
            "exit_code": trace.exit_code,
            "signal": trace.signal,
            "signal_name": _signal_name(trace.signal),
            "timed_out": trace.timed_out,
            "cancelled": trace.cancelled,
            "truncated": budget.truncated,
            "stdout": sanitize_output(bytes(out_buf)),
            "stderr": sanitize_output(bytes(err_buf)),
            "stdout_bytes": len(out_buf),
            "stderr_bytes": len(err_buf),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "cleanup_complete": bool(trace.cleanup_complete),
        }

    @staticmethod
    def _snapshot(trace: _Trace, out_buf: bytearray, err_buf: bytearray, budget: _Budget) -> None:
        trace.stdout_bytes = len(out_buf)
        trace.stderr_bytes = len(err_buf)
        trace.truncated = budget.truncated

    def _record(
        self,
        context: ToolContext,
        trace: _Trace,
        started: float,
        outcome: ShellOutcome,
        reason: ShellRejection | None,
    ) -> None:
        if self._audit is None:
            return
        prepared = trace.prepared
        record = ShellAuditRecord(
            timestamp=datetime.now(UTC),
            call_id=context.call_id,
            tool_name=self.spec.name,
            command=trace.command,
            permission=trace.permission,
            outcome=outcome,
            reason=reason,
            confirmed=trace.confirmed,
            exit_code=trace.exit_code,
            signal=trace.signal,
            argument_digest=prepared.argument_digest if prepared else None,
            argument_count=prepared.argument_count if prepared else None,
            cwd_root=prepared.cwd_root if prepared else None,
            cwd_digest=prepared.cwd_digest if prepared else None,
            stdout_bytes=trace.stdout_bytes,
            stderr_bytes=trace.stderr_bytes,
            truncated=trace.truncated,
            cleanup_complete=trace.cleanup_complete,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
        try:
            self._audit.record(record)
        except Exception as exc:  # an audit failure must not change the run's outcome
            logger.error("shell audit sink failed: %s", type(exc).__name__)


def _classify(trace: _Trace) -> ShellOutcome:
    if trace.cancelled:
        return ShellOutcome.CANCELLED
    if trace.timed_out:
        return ShellOutcome.TIMED_OUT
    if trace.truncated:
        return ShellOutcome.OUTPUT_CAP
    if trace.signal:
        return ShellOutcome.SIGNALED
    if trace.exit_code != 0:
        return ShellOutcome.NONZERO_EXIT
    return ShellOutcome.COMPLETED


def _resolve_cwd(command: AllowedCommand, cwd: object) -> tuple[str, str]:
    """Return (root_name, resolved cwd) or raise; the cwd must stay inside a registered root."""
    if not isinstance(cwd, Mapping) or set(cwd) - {"root", "path"}:
        raise ShellRejected(ShellRejection.INVALID_REQUEST)
    root_name, rel = cwd.get("root"), cwd.get("path", ".")
    if not isinstance(root_name, str) or not isinstance(rel, str):
        raise ShellRejected(ShellRejection.INVALID_REQUEST)
    root = command.cwd_roots.get(root_name)
    if root is None:
        raise ShellRejected(ShellRejection.UNKNOWN_CWD_ROOT)
    if (
        len(rel) > MAX_CWD_PATH_LENGTH
        or has_unsafe_characters(rel)
        or rel.startswith("/")
        or ".." in rel.split("/")
    ):
        raise ShellRejected(ShellRejection.CWD_INVALID_PATH)
    if os.path.realpath(command.root_declared[root_name]) != root:
        raise ShellRejected(ShellRejection.CWD_ROOT_CHANGED)
    resolved = os.path.realpath(os.path.join(root, rel or "."))
    if not _inside(resolved, root):
        raise ShellRejected(ShellRejection.CWD_ESCAPES_ROOT)
    if not os.path.exists(resolved):
        raise ShellRejected(ShellRejection.CWD_NOT_FOUND)
    if not os.path.isdir(resolved):
        raise ShellRejected(ShellRejection.CWD_NOT_DIRECTORY)
    return root_name, resolved


def _verify_executable(command: AllowedCommand) -> None:
    """Refuse when the registered executable was swapped, replaced, or lost its exec bit."""
    try:
        real = os.path.realpath(command.executable)
        info = os.stat(real)
    except OSError:
        raise ShellRejected(ShellRejection.EXECUTABLE_MISSING) from None
    if real != command.resolved_executable or (info.st_dev, info.st_ino) != command.identity:
        raise ShellRejected(ShellRejection.EXECUTABLE_CHANGED)
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK):
        raise ShellRejected(ShellRejection.EXECUTABLE_MISSING)


# Wiring ---------------------------------------------------------------------------------------


def build_shell_tools(
    catalog: CommandCatalog,
    *,
    audit: ShellAuditSink | None = None,
    parent_environ: Mapping[str, str] | None = None,
    term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS,
) -> tuple[ShellTool, ...]:
    """One tool per permission level that has commands, so the registry sees the right level.

    A single ToolSpec carries one permission, so a Yellow `shell.run` and a Red
    `shell.run_confirmed` (and Green `shell.run_readonly`) are separate tools.
    """
    tools = []
    for level in (PermissionLevel.GREEN, PermissionLevel.YELLOW, PermissionLevel.RED):
        if any(c.permission is level for c in catalog.commands):
            tools.append(
                ShellTool(
                    catalog,
                    level,
                    audit=audit,
                    parent_environ=parent_environ,
                    term_grace_seconds=term_grace_seconds,
                )
            )
    return tuple(tools)


def shell_scope_checks(tools: Iterable[ShellTool]) -> dict[str, ScopeCheck]:
    """Scope predicates for `PermissionPolicy`: unknown command, cwd root, or an argument
    grammar violation is out_of_scope before anything runs (and no grant can override it)."""
    return {tool.spec.name: tool.scope_check for tool in tools}


# Builders -------------------------------------------------------------------------------------

# A revision or range (`main..HEAD` is fine), never a path: `/../` segments and `//` are refused so
# the cwd-containment guarantee does not depend on git's own pathspec checks.
_GIT_REV = Matches(r"(?!.*(?:^|/)\.\.(?:/|$))(?!.*//)[A-Za-z0-9_][A-Za-z0-9_./~^@{}:-]{0,99}")
_GIT_FORBIDDEN = (
    r"-c.*",
    r"-C.*",
    r"-O.*",
    r"--config.*",
    r"--exec.*",
    r"--upload-pack.*",
    r"--receive-pack.*",
    r"--git-dir.*",
    r"--work-tree.*",
    r"--namespace.*",
    r"--super-prefix.*",
    r"--ext-diff",
    r"--textconv",
    r"--output.*",
    r"--open-files-in-pager.*",
    r"--pager.*",
    r"--paginate",
    r"--no-index",
)
_GIT_PATHS = RelPath()
_GIT_NO_EXEC = ("--no-ext-diff", "--no-textconv")


def _flags(*names: str) -> tuple[FlagRule, ...]:
    return tuple(FlagRule(name=n) for n in names)


def git_readonly_command(
    executable: str,
    cwd_roots: Mapping[str, str],
    *,
    name: str = "git_ro",
    timeout_seconds: float = 30.0,
    max_output_bytes: int = 32_768,
) -> AllowedCommand:
    """Conservative git: `status`, `log`, `diff` with a bounded flag set (no write flags). Yellow,
    and `read_only=False` on purpose: the flag would claim no side effects, which repository
    config can defeat.

    Not registered by default. No global options, aliases, `-c`, `--exec-path`, upload/receive
    pack, external diff, textconv, pager, or output-to-file. The application pins config with
    trusted `-c` overrides and fixed GIT_* values, and no GIT_* name is inherited. Reading a
    repository whose own config you do not control can still run programs it configures (filter
    drivers, for example): read-only here means no write flags, not no side effects.
    """
    count = IntRange(1, 200)
    revs = {"positionals": _GIT_REV}
    paths = {"after_double_dash": _GIT_PATHS, "max_after_double_dash": 16}
    policy = ArgumentPolicy(
        forbidden=_GIT_FORBIDDEN,
        subcommands={
            "status": ArgumentPolicy(
                flags=(
                    *_flags("-s", "--short", "-b", "--branch", "--porcelain", "--no-renames"),
                    FlagRule(
                        name="--untracked-files",
                        value=OneOf(("no", "normal", "all")),
                        style=ValueStyle.JOINED,
                    ),
                ),
                **paths,
            ),
            "log": ArgumentPolicy(
                flags=(
                    *_flags(
                        "--oneline",
                        "--stat",
                        "--name-only",
                        "--name-status",
                        "--no-merges",
                        "--no-color",
                        "--decorate",
                        "--graph",
                    ),
                    FlagRule(name="-n", value=count),
                    FlagRule(name="--max-count", value=count, style=ValueStyle.JOINED),
                    FlagRule(pattern=r"-[0-9]{1,3}"),
                ),
                max_positionals=4,
                inject_args=_GIT_NO_EXEC,
                **revs,
                **paths,
            ),
            "diff": ArgumentPolicy(
                flags=(
                    *_flags(
                        "--stat",
                        "--numstat",
                        "--shortstat",
                        "--name-only",
                        "--name-status",
                        "--cached",
                        "--staged",
                        "--no-color",
                        "--no-renames",
                    ),
                    FlagRule(name="-U", value=IntRange(0, 50)),
                    FlagRule(name="--unified", value=IntRange(0, 50), style=ValueStyle.JOINED),
                ),
                max_positionals=2,
                inject_args=_GIT_NO_EXEC,
                **revs,
                **paths,
            ),
        },
    )
    return AllowedCommand(
        name=name,
        executable=executable,
        permission=PermissionLevel.YELLOW,
        argument_policy=policy,
        cwd_roots=cwd_roots,
        env_allowlist=(),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        read_only=False,  # no write flags, but repository config can still run programs
        fixed_args=(
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-c",
            "log.showSignature=false",
        ),
        env_fixed={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_ATTR_NOSYSTEM": "1",
        },
    )


_PYTEST_FORBIDDEN = (
    r"-p.*",
    r"-c.*",
    r"-o.*",
    r"--override-ini.*",
    r"--rootdir.*",
    r"--confcutdir.*",
    r"--basetemp.*",
    r"--junit.*",
    r"--result-?log.*",
    r"--pdb.*",
    r"--trace",
    r"--debug.*",
    r"--import-mode.*",
    r"--pyargs",
    r"--log-file.*",
    r"--doctest.*",
    r"--cache.*",
)
_PYTEST_EXPR = Matches(r"[A-Za-z0-9_ ().\[\]:-]{1,200}")


def pytest_command(
    python_executable: str,
    cwd_roots: Mapping[str, str],
    *,
    name: str = "pytest",
    timeout_seconds: float = 300.0,
    max_output_bytes: int = 32_768,
    autoload_plugins: bool = False,
) -> AllowedCommand:
    """`python -m pytest` with a bounded flag set. Yellow. Not registered by default.

    WARNING: pytest imports and runs project code (test modules, conftest.py, plugins named in
    the project's own config). Allow-listing its flags only limits what the model can add; it
    does not make the run safe. Register it only for projects you would run yourself, and treat
    the output as untrusted. Plugin autoload is disabled unless `autoload_plugins=True`.
    `python_executable` is resolved with realpath, so a venv interpreter becomes its base
    interpreter and loses the venv's packages; register an interpreter that has pytest.
    """
    policy = ArgumentPolicy(
        forbidden=_PYTEST_FORBIDDEN,
        flags=(
            *_flags("-q", "-v", "-x", "--no-header", "--disable-warnings"),
            FlagRule(name="--maxfail", value=IntRange(1, 100), style=ValueStyle.JOINED),
            FlagRule(name="--durations", value=IntRange(0, 100), style=ValueStyle.JOINED),
            FlagRule(
                name="--tb",
                value=OneOf(("auto", "long", "short", "line", "native", "no")),
                style=ValueStyle.JOINED,
            ),
            FlagRule(name="-k", value=_PYTEST_EXPR),
            FlagRule(name="-m", value=_PYTEST_EXPR),
        ),
        positionals=RelPath(),
        max_positionals=16,
    )
    env_fixed = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    if not autoload_plugins:
        env_fixed["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return AllowedCommand(
        name=name,
        executable=python_executable,
        permission=PermissionLevel.YELLOW,
        argument_policy=policy,
        cwd_roots=cwd_roots,
        env_allowlist=(),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        fixed_args=("-m", "pytest", "-p", "no:cacheprovider", "--confcutdir=."),
        env_fixed=env_fixed,
    )
