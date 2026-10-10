"""Range-limited filesystem WRITE tools (`fs.write_file`, `fs.append`, `fs.make_dir`, `fs.move`,
`fs.delete`).

Nothing registers these by default. Application code must call `register_filesystem_write_tools`
with explicit roots. Every tool is Yellow or Red, so the permission policy demands a human
confirmation; each tool additionally refuses to run when the registry did not accept a grant
(`context.confirmed`), so putting a tool in `allow_yellow` does not turn it into an unattended one.

Path safety is the read tools' (`backend.tools.filesystem`): lexical validation, credential-shaped
name deny-list, `realpath` containment, then descriptor-relative walking with `O_NOFOLLOW`.
On top of that the write tools have a non-overridable deny-list for databases, vault and secrets
shaped names and their own bookkeeping names (trash directory, temp files).

Content never travels in the tool arguments. The application stages text in a `ContentStage`
and the call carries only `content_sha256` and `content_size`; those are what the human approval
digest covers and what the approval summary shows. `delete` never removes anything physically: it
renames the entry into a quarantine directory inside the same root.
"""

import asyncio
import errno
import fnmatch
import hashlib
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.tools.contract import (
    CancellationToken,
    PermissionLevel,
    Tool,
    ToolContext,
    ToolSpec,
)
from backend.tools.filesystem import (
    _CLOEXEC,
    _NOCTTY,
    _ROOT_PROP,
    SAFE_OPEN_SUPPORTED,
    FilesystemReason,
    FilesystemRoot,
    FilesystemToolError,
    _Budget,
    _close,
    _directory,
    _fail,
    _fold,
    _lexical,
    _open_dir_at,
    _open_root,
    _os_failure,
    _out_path,
    _path_prop,
    _root_table,
)
from backend.tools.permission import ScopeCheck
from backend.tools.registry import ToolRegistry

FS_WRITE_FILE = "fs.write_file"
FS_APPEND = "fs.append"
FS_MAKE_DIR = "fs.make_dir"
FS_MOVE = "fs.move"
FS_DELETE = "fs.delete"

TRASH_DIR = ".jarvis-trash"
TMP_PREFIX = ".jarvis-tmp-"
HARD_MAX_WRITE_BYTES = 1024 * 1024
MAX_CREATED_DIRS = 8

# Names the write tools never touch, on top of the read tools' credential deny-list. Matched
# case-insensitively (NFC + casefold) against every path component. Not overridable per root.
WRITE_DENIED_PATTERNS: tuple[str, ...] = (
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.sqlite-*",
    "*.sqlite3-*",
    "*.db-*",
    "*-wal",
    "*-shm",
    "*-journal",
    "vault",
    "vault.*",
    "*.vault",
    ".vault*",
    ".obsidian",
    "secret",
    "secret.*",
    "secrets",
    "secrets.*",
    "*.secret",
    "*.secrets",
    TRASH_DIR,
    TMP_PREFIX + "*",
)

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class WriteLimits:
    max_write_bytes: int = 256 * 1024
    max_file_bytes: int = HARD_MAX_WRITE_BYTES
    max_seconds: float = 5.0

    def __post_init__(self) -> None:
        for value in (self.max_write_bytes, self.max_file_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("limits must be positive integers")
        if self.max_write_bytes > HARD_MAX_WRITE_BYTES or self.max_file_bytes > 16 * 1024 * 1024:
            raise ValueError("limits exceed the hard caps")
        if isinstance(self.max_seconds, bool) or not 0 < float(self.max_seconds) <= 60:
            raise ValueError("max_seconds must be in (0, 60]")


DEFAULT_WRITE_LIMITS = WriteLimits()


# Staged content -------------------------------------------------------------------------


@dataclass(frozen=True)
class StagedContent:
    """What goes into a tool call instead of the text itself."""

    content_sha256: str
    content_size: int


class ContentStage:
    """In-memory, bounded, expiring store of text the application wants a write tool to use.

    Application code stages text and builds the call from the returned hash and size. The
    approval digest and summary therefore never contain the text. Entries are looked up by hash
    and re-verified by the tool, so a wrong or tampered entry cannot be written.
    """

    def __init__(
        self,
        *,
        max_entries: int = 32,
        max_total_bytes: int = 4 * 1024 * 1024,
        ttl_seconds: float = 900.0,
        max_item_bytes: int = HARD_MAX_WRITE_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._max_total = max_total_bytes
        self._ttl = ttl_seconds
        self._max_item = max_item_bytes
        self._now = monotonic
        self._lock = threading.Lock()
        self._items: dict[str, tuple[bytes, float]] = {}

    def _prune(self) -> None:
        now = self._now()
        for key in [k for k, (_, expiry) in self._items.items() if expiry <= now]:
            del self._items[key]

    def put(self, text: str) -> StagedContent:
        if not isinstance(text, str):
            raise TypeError("staged content must be text")
        try:
            data = text.encode("utf-8")
        except UnicodeError:
            raise ValueError("content is not valid text") from None
        if b"\x00" in data or len(data) > self._max_item:
            raise ValueError("content is too large or contains NUL")
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._prune()
            if digest not in self._items:
                total = sum(len(v) for v, _ in self._items.values())
                if len(self._items) >= self._max_entries or total + len(data) > self._max_total:
                    raise ValueError("content stage is full")
            self._items[digest] = (data, self._now() + self._ttl)
        return StagedContent(digest, len(data))

    def get(self, digest: str, size: int) -> bytes | None:
        with self._lock:
            self._prune()
            item = self._items.get(digest)
        if item is None:
            return None
        data = item[0]
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            return None
        return data

    def discard(self, digest: str) -> None:
        with self._lock:
            self._items.pop(digest, None)

    def __len__(self) -> int:
        with self._lock:
            self._prune()
            return len(self._items)


# Lexical checks -------------------------------------------------------------------------


def _write_denied(extra: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    for part in parts:
        folded = _fold(part)
        if any(fnmatch.fnmatchcase(folded, pat) for pat in (*WRITE_DENIED_PATTERNS, *extra)):
            return True
    return False


def _normalise_extra(patterns: Iterable[str]) -> tuple[str, ...]:
    out = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or "/" in pattern:
            raise ValueError("extra denied patterns must be non-empty name patterns")
        out.append(_fold(pattern))
    return tuple(out)


def _target(
    table: Mapping[str, FilesystemRoot],
    extra: tuple[str, ...],
    arguments: Mapping[str, Any],
    key: str = "path",
) -> tuple[FilesystemRoot, tuple[str, ...]]:
    probe = {"root": arguments.get("root"), "path": arguments.get(key, "")}
    root, parts = _lexical(table, probe, allow_root=False)
    if _write_denied(extra, parts):
        raise _fail(FilesystemReason.DENIED_NAME)
    return root, parts


def _check_content_args(arguments: Mapping[str, Any], limits: WriteLimits) -> tuple[str, int]:
    digest = arguments.get("content_sha256")
    size = arguments.get("content_size")
    if (
        not isinstance(digest, str)
        or _SHA_RE.fullmatch(digest) is None
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise _fail(FilesystemReason.INVALID_PATH)
    if size > limits.max_write_bytes:
        raise _fail(FilesystemReason.TOO_LARGE)
    return digest, size


def _check_move(
    table: Mapping[str, FilesystemRoot], extra: tuple[str, ...], arguments: Mapping[str, Any]
) -> tuple[FilesystemRoot, tuple[str, ...], tuple[str, ...]]:
    root, src = _target(table, extra, arguments, "path")
    root2, dst = _target(table, extra, arguments, "to_path")
    if root2 is not root or src == dst or dst[: len(src)] == src:
        raise _fail(FilesystemReason.INVALID_PATH)  # same entry, or into its own subtree
    return root, src, dst


def filesystem_write_scope_checks(
    roots: Iterable[FilesystemRoot],
    *,
    limits: WriteLimits = DEFAULT_WRITE_LIMITS,
    extra_denied_patterns: Iterable[str] = (),
) -> dict[str, ScopeCheck]:
    """Policy scope predicates: unknown root, malformed or denied path => `out_of_scope`.

    Pass as `PermissionPolicy(scope_checks=...)`. A failed scope check beats any grant, so a
    denied path never even reaches the approval queue. The tools re-check on their own.
    """
    table = _root_table(roots)
    extra = _normalise_extra(extra_denied_patterns)

    def wrap(fn: Callable[[Mapping[str, Any]], object]) -> ScopeCheck:
        def check(spec: ToolSpec, arguments: Mapping[str, Any]) -> bool:
            try:
                fn(arguments)
            except FilesystemToolError:
                return False
            return True

        return check

    def content(arguments: Mapping[str, Any]) -> None:
        _target(table, extra, arguments)
        _check_content_args(arguments, limits)

    return {
        FS_WRITE_FILE: wrap(content),
        FS_APPEND: wrap(content),
        FS_MAKE_DIR: wrap(lambda a: _target(table, extra, a)),
        FS_MOVE: wrap(lambda a: _check_move(table, extra, a)),
        FS_DELETE: wrap(lambda a: _target(table, extra, a)),
    }


# Descriptor-relative primitives -----------------------------------------------------------


def _lstat_opt(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _os_failure(exc) from None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise _fail(FilesystemReason.IO_ERROR)
        view = view[written:]


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass  # best effort; some filesystems refuse fsync on directories


def _regular_or_none(info: os.stat_result | None, device: int) -> os.stat_result | None:
    """Return `info` if it is a regular file on the root's device; fail on anything else."""
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise _fail(FilesystemReason.OUTSIDE_ROOT)
    if stat.S_ISDIR(info.st_mode):
        raise _fail(FilesystemReason.NOT_A_FILE)
    if not stat.S_ISREG(info.st_mode):
        raise _fail(FilesystemReason.SPECIAL_FILE)
    if info.st_dev != device:
        raise _fail(FilesystemReason.OUTSIDE_ROOT)
    return info


def _make_temp(dir_fd: int, mode: int) -> tuple[int, str]:
    for _ in range(8):
        name = TMP_PREFIX + secrets.token_hex(8)
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC | _NOCTTY,
                mode,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise _os_failure(exc) from None
        return fd, name
    raise _fail(FilesystemReason.IO_ERROR)


def _unlink_quiet(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _atomic_write(
    dir_fd: int, name: str, data: bytes, overwrite: bool, device: int, budget: _Budget
) -> bool:
    """Write via temp file in the same directory, then publish atomically. Returns `replaced`."""
    existing = _regular_or_none(_lstat_opt(dir_fd, name), device)
    if existing is not None and not overwrite:
        raise _fail(FilesystemReason.ALREADY_EXISTS)
    mode = stat.S_IMODE(existing.st_mode) & 0o666 if existing is not None else 0o600
    tmp_fd, tmp_name = _make_temp(dir_fd, 0o600)
    try:
        try:
            _write_all(tmp_fd, data)
            os.fchmod(tmp_fd, mode)
            _fsync(tmp_fd)
        finally:
            _close(tmp_fd)
        budget.check_cancelled()
        if existing is not None:
            # Replacing a name never follows it: a symlink swapped in later is replaced, not
            # written through. A directory swapped in makes rename fail.
            os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            replaced = True
        else:
            try:
                os.link(
                    tmp_name,
                    name,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise _fail(FilesystemReason.ALREADY_EXISTS) from None
            _unlink_quiet(dir_fd, tmp_name)
            replaced = False
        tmp_name = ""
        _fsync(dir_fd)
        return replaced
    except OSError as exc:
        raise _os_failure(exc) from None
    finally:
        if tmp_name:
            _unlink_quiet(dir_fd, tmp_name)


def _append(dir_fd: int, name: str, data: bytes, device: int, max_file: int) -> int:
    before = _regular_or_none(_lstat_opt(dir_fd, name), device)
    if before is None:
        raise _fail(FilesystemReason.NOT_FOUND)
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | _O_NONBLOCK | _NOCTTY | _CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        raise _os_failure(exc) from None
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise _fail(FilesystemReason.CHANGED)
        if after.st_nlink > 1:
            # A hard link may be an alias of a file outside the root; writing in place is unsafe.
            raise _fail(FilesystemReason.OUTSIDE_ROOT)
        if after.st_size + len(data) > max_file:
            raise _fail(FilesystemReason.TOO_LARGE)
        _write_all(fd, data)
        _fsync(fd)
        return after.st_size + len(data)
    except OSError as exc:
        raise _os_failure(exc) from None
    finally:
        _close(fd)


def _make_dirs(root: FilesystemRoot, parts: tuple[str, ...], parents: bool) -> None:
    if len(parts) > 16:
        raise _fail(FilesystemReason.INVALID_PATH)
    fd = _open_root(root)
    created = 0
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if last:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    raise _fail(FilesystemReason.ALREADY_EXISTS) from None
                except OSError as exc:
                    raise _os_failure(exc) from None
                _fsync(fd)
                return
            try:
                child = _open_dir_at(fd, part, root.identity[0])
            except FilesystemToolError as exc:
                if exc.reason is not FilesystemReason.NOT_FOUND or not parents:
                    raise
                created += 1
                if created > MAX_CREATED_DIRS:
                    raise _fail(FilesystemReason.INVALID_PATH) from None
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as os_exc:
                    raise _os_failure(os_exc) from None
                child = _open_dir_at(fd, part, root.identity[0])
            _close(fd)
            fd = child
    finally:
        _close(fd)


def _relocate(src_fd: int, src_name: str, dst_fd: int, dst_name: str, device: int) -> None:
    """Move without ever replacing an existing destination (best effort for directories)."""
    info = _lstat_opt(src_fd, src_name)
    if info is None:
        raise _fail(FilesystemReason.NOT_FOUND)
    if info.st_dev != device:
        raise _fail(FilesystemReason.OUTSIDE_ROOT)
    try:
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            # A symlink is moved as a link (never followed); it stays inside the root.
            try:
                os.link(
                    src_name,
                    dst_name,
                    src_dir_fd=src_fd,
                    dst_dir_fd=dst_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise _fail(FilesystemReason.ALREADY_EXISTS) from None
            linked = os.stat(dst_name, dir_fd=dst_fd, follow_symlinks=False)
            now = _lstat_opt(src_fd, src_name)
            if now is not None and (now.st_dev, now.st_ino) == (linked.st_dev, linked.st_ino):
                os.unlink(src_name, dir_fd=src_fd)
            else:  # the source was swapped meanwhile: undo, leave the new source alone
                os.unlink(dst_name, dir_fd=dst_fd)
                raise _fail(FilesystemReason.CHANGED)
        elif stat.S_ISDIR(info.st_mode):
            if _lstat_opt(dst_fd, dst_name) is not None:
                raise _fail(FilesystemReason.ALREADY_EXISTS)
            os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        else:
            raise _fail(FilesystemReason.SPECIAL_FILE)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTEMPTY, errno.EEXIST):
            raise _fail(FilesystemReason.ALREADY_EXISTS) from None
        raise _os_failure(exc) from None
    _fsync(src_fd)
    _fsync(dst_fd)


def _trash_name(basename: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    clipped = basename.encode("utf-8")[:120].decode("utf-8", "ignore")
    return f"{stamp}-{secrets.token_hex(4)}-{clipped}"


# Tools ------------------------------------------------------------------------------------

_HASH_PROP = {
    "type": "string",
    "minLength": 64,
    "maxLength": 64,
    "description": "SHA-256 (lowercase hex) of the UTF-8 content staged by the application.",
}


def _size_prop(limits: WriteLimits) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 0,
        "maximum": limits.max_write_bytes,
        "description": "Size in bytes of the staged content.",
    }


class _WriteTool:
    spec: ToolSpec

    def __init__(
        self,
        table: Mapping[str, FilesystemRoot],
        limits: WriteLimits,
        extra: tuple[str, ...],
        stage: ContentStage,
    ) -> None:
        if not SAFE_OPEN_SUPPORTED:
            raise FilesystemToolError(FilesystemReason.UNSUPPORTED)
        self._table = dict(table)
        self._limits = limits
        self._extra = extra
        self._stage = stage

    def _note(self) -> str:
        return (
            " Roots: " + ", ".join(sorted(self._table)) + ". Requires human confirmation. "
            "Credential-, database-, vault- and secret-shaped names are refused."
        )

    async def run(self, arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        if not context.confirmed:
            raise _fail(FilesystemReason.UNCONFIRMED)
        token = context.cancellation
        return await asyncio.to_thread(self._guarded, arguments, token)

    def _guarded(self, arguments: Mapping[str, Any], token: CancellationToken) -> dict[str, Any]:
        try:
            return self._execute(arguments, _Budget(token, self._limits.max_seconds))
        except FilesystemToolError:
            raise
        except OSError as exc:
            raise _os_failure(exc) from None
        except (UnicodeError, ValueError):
            raise _fail(FilesystemReason.IO_ERROR) from None

    def _execute(self, arguments: Mapping[str, Any], budget: _Budget) -> dict[str, Any]:
        raise NotImplementedError

    def _content(self, arguments: Mapping[str, Any]) -> tuple[str, bytes]:
        digest, size = _check_content_args(arguments, self._limits)
        data = self._stage.get(digest, size)
        if data is None:
            raise _fail(FilesystemReason.CONTENT_UNAVAILABLE)
        return digest, data


def _content_output() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "root": {"type": "string", "maxLength": 32},
            "path": _out_path(),
            "bytes": {"type": "integer", "minimum": 0},
            "sha256": {"type": "string", "maxLength": 64},
            "replaced": {"type": "boolean"},
        },
        "required": ["root", "path", "bytes", "sha256", "replaced"],
    }


class WriteFileTool(_WriteTool):
    """`fs.write_file`: atomically create a file, or replace one only with `overwrite=true`."""

    def __init__(self, table, limits, extra, stage) -> None:  # type: ignore[no-untyped-def]
        super().__init__(table, limits, extra, stage)
        self.spec = ToolSpec(
            name=FS_WRITE_FILE,
            description=(
                "Create a text file under a configured root from application-staged content "
                "(identified by SHA-256 and size; the text itself is not part of the call). "
                "Fails if the file exists unless `overwrite` is true. The write is atomic "
                "(temp file then rename); the parent directory must exist." + self._note()
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "root": _ROOT_PROP,
                    "path": _path_prop("File path relative to the root."),
                    "content_sha256": _HASH_PROP,
                    "content_size": _size_prop(limits),
                    "overwrite": {"type": "boolean", "description": "Allow replacing a file."},
                },
                "required": ["root", "path", "content_sha256", "content_size", "overwrite"],
            },
            output_schema=_content_output(),
            permission=PermissionLevel.RED,
            environment="local",
            timeout_seconds=max(10.0, limits.max_seconds * 2),
        )

    def _execute(self, arguments, budget):  # type: ignore[no-untyped-def]
        overwrite = arguments.get("overwrite")
        if not isinstance(overwrite, bool):
            raise _fail(FilesystemReason.INVALID_PATH)
        root, parts = _target(self._table, self._extra, arguments)
        digest, data = self._content(arguments)
        with _directory(root, parts[:-1]) as dfd:
            replaced = _atomic_write(dfd, parts[-1], data, overwrite, root.identity[0], budget)
        self._stage.discard(digest)
        return {
            "root": root.name,
            "path": "/".join(parts),
            "bytes": len(data),
            "sha256": digest,
            "replaced": replaced,
        }


class AppendTool(_WriteTool):
    """`fs.append`: append staged text to an existing regular file."""

    def __init__(self, table, limits, extra, stage) -> None:  # type: ignore[no-untyped-def]
        super().__init__(table, limits, extra, stage)
        self.spec = ToolSpec(
            name=FS_APPEND,
            description=(
                "Append application-staged text to an EXISTING file under a configured root. "
                "The file must be a regular, singly-linked file; the result must stay under the "
                "per-file cap. Not atomic: a crash can leave a partial append." + self._note()
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "root": _ROOT_PROP,
                    "path": _path_prop("File path relative to the root."),
                    "content_sha256": _HASH_PROP,
                    "content_size": _size_prop(limits),
                },
                "required": ["root", "path", "content_sha256", "content_size"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "root": {"type": "string", "maxLength": 32},
                    "path": _out_path(),
                    "appended_bytes": {"type": "integer", "minimum": 0},
                    "file_size": {"type": "integer", "minimum": 0},
                },
                "required": ["root", "path", "appended_bytes", "file_size"],
            },
            permission=PermissionLevel.YELLOW,
            environment="local",
            timeout_seconds=max(10.0, limits.max_seconds * 2),
        )

    def _execute(self, arguments, budget):  # type: ignore[no-untyped-def]
        root, parts = _target(self._table, self._extra, arguments)
        digest, data = self._content(arguments)
        with _directory(root, parts[:-1]) as dfd:
            total = _append(dfd, parts[-1], data, root.identity[0], self._limits.max_file_bytes)
        self._stage.discard(digest)
        return {
            "root": root.name,
            "path": "/".join(parts),
            "appended_bytes": len(data),
            "file_size": total,
        }


class MakeDirTool(_WriteTool):
    """`fs.make_dir`: create a directory (optionally with a few missing parents)."""

    def __init__(self, table, limits, extra, stage) -> None:  # type: ignore[no-untyped-def]
        super().__init__(table, limits, extra, stage)
        self.spec = ToolSpec(
            name=FS_MAKE_DIR,
            description=(
                "Create a new directory under a configured root. Fails if it exists. With "
                f"`parents` up to {MAX_CREATED_DIRS} missing parents are created." + self._note()
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "root": _ROOT_PROP,
                    "path": _path_prop("Directory path relative to the root."),
                    "parents": {"type": "boolean", "description": "Create missing parents."},
                },
                "required": ["root", "path", "parents"],
            },
            output_schema={
                "type": "object",
                "properties": {"root": {"type": "string", "maxLength": 32}, "path": _out_path()},
                "required": ["root", "path"],
            },
            permission=PermissionLevel.YELLOW,
            environment="local",
            timeout_seconds=max(10.0, limits.max_seconds * 2),
        )

    def _execute(self, arguments, budget):  # type: ignore[no-untyped-def]
        parents = arguments.get("parents")
        if not isinstance(parents, bool):
            raise _fail(FilesystemReason.INVALID_PATH)
        root, parts = _target(self._table, self._extra, arguments)
        _make_dirs(root, parts, parents)
        return {"root": root.name, "path": "/".join(parts)}


class MoveTool(_WriteTool):
    """`fs.move`: rename a file or directory inside one root, never replacing anything."""

    def __init__(self, table, limits, extra, stage) -> None:  # type: ignore[no-untyped-def]
        super().__init__(table, limits, extra, stage)
        self.spec = ToolSpec(
            name=FS_MOVE,
            description=(
                "Move or rename a file or directory within one configured root. The destination "
                "must not exist and its parent must; nothing is overwritten." + self._note()
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "root": _ROOT_PROP,
                    "path": _path_prop("Existing entry relative to the root."),
                    "to_path": _path_prop("New location relative to the same root."),
                },
                "required": ["root", "path", "to_path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "root": {"type": "string", "maxLength": 32},
                    "path": _out_path(),
                    "to_path": _out_path(),
                },
                "required": ["root", "path", "to_path"],
            },
            permission=PermissionLevel.YELLOW,
            environment="local",
            timeout_seconds=max(10.0, limits.max_seconds * 2),
        )

    def _execute(self, arguments, budget):  # type: ignore[no-untyped-def]
        root, src, dst = _check_move(self._table, self._extra, arguments)
        device = root.identity[0]
        with _directory(root, src[:-1]) as sfd, _directory(root, dst[:-1]) as dfd:
            _relocate(sfd, src[-1], dfd, dst[-1], device)
        return {"root": root.name, "path": "/".join(src), "to_path": "/".join(dst)}


class DeleteTool(_WriteTool):
    """`fs.delete`: quarantine an entry into the root's trash directory. Never unlinks data."""

    def __init__(self, table, limits, extra, stage) -> None:  # type: ignore[no-untyped-def]
        super().__init__(table, limits, extra, stage)
        self.spec = ToolSpec(
            name=FS_DELETE,
            description=(
                f"'Delete' a file or directory by moving it into `{TRASH_DIR}/` at the top of "
                "its root. Nothing is physically removed; a human can restore it. The trash "
                "itself cannot be read through the write tools." + self._note()
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "root": _ROOT_PROP,
                    "path": _path_prop("Entry relative to the root."),
                },
                "required": ["root", "path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "root": {"type": "string", "maxLength": 32},
                    "path": _out_path(),
                    "trashed_to": _out_path(),
                },
                "required": ["root", "path", "trashed_to"],
            },
            permission=PermissionLevel.YELLOW,
            environment="local",
            timeout_seconds=max(10.0, limits.max_seconds * 2),
        )

    def _execute(self, arguments, budget):  # type: ignore[no-untyped-def]
        root, parts = _target(self._table, self._extra, arguments)
        device = root.identity[0]
        trash_name = _trash_name(parts[-1])
        rfd = _open_root(root)
        try:
            try:
                os.mkdir(TRASH_DIR, 0o700, dir_fd=rfd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _os_failure(exc) from None
            tfd = _open_dir_at(rfd, TRASH_DIR, device)  # refuses a symlinked trash
        finally:
            _close(rfd)
        try:
            with _directory(root, parts[:-1]) as sfd:
                _relocate(sfd, parts[-1], tfd, trash_name, device)
        finally:
            _close(tfd)
        return {
            "root": root.name,
            "path": "/".join(parts),
            "trashed_to": f"{TRASH_DIR}/{trash_name}",
        }


# Registration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FilesystemWriteToolset:
    tools: tuple[Tool, ...]
    stage: ContentStage
    scope_checks: Mapping[str, ScopeCheck]


def filesystem_write_tools(
    roots: Iterable[FilesystemRoot],
    *,
    stage: ContentStage | None = None,
    limits: WriteLimits = DEFAULT_WRITE_LIMITS,
    extra_denied_patterns: Iterable[str] = (),
) -> FilesystemWriteToolset:
    """Build (not register) the write tools for explicitly supplied roots."""
    roots = tuple(roots)
    table = _root_table(roots)
    extra = _normalise_extra(extra_denied_patterns)
    stage = stage if stage is not None else ContentStage(max_item_bytes=limits.max_write_bytes)
    tools: tuple[Tool, ...] = (
        WriteFileTool(table, limits, extra, stage),
        AppendTool(table, limits, extra, stage),
        MakeDirTool(table, limits, extra, stage),
        MoveTool(table, limits, extra, stage),
        DeleteTool(table, limits, extra, stage),
    )
    checks = filesystem_write_scope_checks(
        roots, limits=limits, extra_denied_patterns=extra_denied_patterns
    )
    return FilesystemWriteToolset(tools, stage, checks)


def register_filesystem_write_tools(
    registry: ToolRegistry,
    roots: Iterable[FilesystemRoot],
    *,
    stage: ContentStage | None = None,
    limits: WriteLimits = DEFAULT_WRITE_LIMITS,
    extra_denied_patterns: Iterable[str] = (),
) -> FilesystemWriteToolset:
    """Register the five write tools. All or nothing. Only ever called by application code."""
    toolset = filesystem_write_tools(
        roots, stage=stage, limits=limits, extra_denied_patterns=extra_denied_patterns
    )
    added: list[str] = []
    try:
        for tool in toolset.tools:
            registry.register(tool)
            added.append(tool.spec.name)
    except Exception:
        for name in added:
            registry.unregister(name)
        raise
    return toolset


__all__ = [
    "DEFAULT_WRITE_LIMITS",
    "FS_APPEND",
    "FS_DELETE",
    "FS_MAKE_DIR",
    "FS_MOVE",
    "FS_WRITE_FILE",
    "TMP_PREFIX",
    "TRASH_DIR",
    "WRITE_DENIED_PATTERNS",
    "ContentStage",
    "FilesystemWriteToolset",
    "StagedContent",
    "WriteLimits",
    "filesystem_write_scope_checks",
    "filesystem_write_tools",
    "register_filesystem_write_tools",
]
