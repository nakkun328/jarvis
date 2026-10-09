"""Filesystem write tools, exercised only inside pytest temporary directories."""

import asyncio
import hashlib
import os
import stat
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import backend.tools.filesystem as fsmod
import backend.tools.filesystem_write as fw
from backend.core.database import Database
from backend.tools.approvals import ApprovalState, ApprovalStore, summarize_arguments
from backend.tools.confirmed import ConfirmedExecutor
from backend.tools.contract import (
    CancellationToken,
    PermissionLevel,
    ToolCall,
    ToolContext,
    ToolErrorCode,
    ToolStatus,
    digest_arguments,
)
from backend.tools.filesystem import FilesystemReason, FilesystemRoot, FilesystemToolError
from backend.tools.filesystem_write import (
    FS_APPEND,
    FS_DELETE,
    FS_MAKE_DIR,
    FS_MOVE,
    FS_WRITE_FILE,
    TRASH_DIR,
    ContentStage,
    WriteLimits,
    filesystem_write_scope_checks,
    filesystem_write_tools,
    register_filesystem_write_tools,
)
from backend.tools.permission import PermissionPolicy
from backend.tools.registry import DuplicateToolError, ToolRegistry

REPO = Path(__file__).resolve().parents[1]
START = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
SECRET_TEXT = "TOP-SECRET-BODY-8c1f"
OUTSIDE = "OUTSIDE-ORIGINAL-4f1c"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def run(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.root_path = tmp / "root"
        self.root_path.mkdir()
        self.other_path = tmp / "other"
        self.other_path.mkdir()
        self.outside = tmp / "outside"
        self.outside.mkdir()
        (self.outside / "victim.txt").write_text(OUTSIDE, encoding="utf-8")
        self.root = FilesystemRoot("docs", self.root_path)
        self.other = FilesystemRoot("other", self.other_path)
        self.clock = Clock()
        self.db = Database(tmp / "approvals.sqlite3")
        self.db.initialize()
        self.store = ApprovalStore(self.db, ttl_seconds=60, clock=self.clock)
        self.stage = ContentStage()
        self.toolset = filesystem_write_tools([self.root, self.other], stage=self.stage)
        self.tools = {t.spec.name: t for t in self.toolset.tools}
        policy = PermissionPolicy(scope_checks=self.toolset.scope_checks, clock=self.clock)
        self.registry = ToolRegistry(policy, cancel_grace_seconds=0.1)
        for tool in self.toolset.tools:
            self.registry.register(tool)
        self.executor = ConfirmedExecutor(self.registry, self.store.requester(), clock=self.clock)
        self.n = 0

    # Direct invocation (as if the registry accepted a grant).
    def direct(self, name: str, arguments: dict, *, confirmed: bool = True):
        context = ToolContext(
            "direct", name, self.tools[name].spec.permission, confirmed, 5.0, CancellationToken()
        )
        return run(self.tools[name].run(arguments, context))

    def stage_text(self, text: str) -> dict:
        staged = self.stage.put(text)
        return {"content_sha256": staged.content_sha256, "content_size": staged.content_size}

    def write_args(self, path: str, text: str, *, overwrite: bool = False) -> dict:
        return {"root": "docs", "path": path, "overwrite": overwrite, **self.stage_text(text)}

    def call(self, tool: str, arguments: dict, call_id: str | None = None) -> ToolCall:
        self.n += 1
        return ToolCall(call_id or f"c{self.n}", tool, arguments)

    def approved(self, tool: str, arguments: dict):
        """Full flow: request, human approves, run. Returns the final outcome."""
        first = run(self.executor.execute(self.call(tool, arguments)))
        assert first.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
        self.store.decide(first.approval_id, True)
        return run(self.executor.execute(self.call(tool, arguments)))


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def reason_of(excinfo) -> FilesystemReason:
    assert isinstance(excinfo.value, FilesystemToolError)
    return excinfo.value.reason


def no_temp_files(path: Path) -> bool:
    return not [p for p in path.rglob("*") if p.name.startswith(fw.TMP_PREFIX)]


# Specs and registration -----------------------------------------------------------------


def test_permission_levels_require_confirmation(env: Env) -> None:
    levels = {n: t.spec.permission for n, t in env.tools.items()}
    assert levels[FS_WRITE_FILE] is PermissionLevel.RED
    assert all(v is not PermissionLevel.GREEN for v in levels.values())
    assert set(levels) == {FS_WRITE_FILE, FS_APPEND, FS_MAKE_DIR, FS_MOVE, FS_DELETE}


def test_nothing_registers_write_tools_by_default() -> None:
    for path in (REPO / "backend").rglob("*.py"):
        if "tools" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "filesystem_write" not in text, path.name
    for path in (REPO / "backend" / "tools").glob("*.py"):
        if path.name != "filesystem_write.py":
            assert "filesystem_write" not in path.read_text(encoding="utf-8"), path.name
    assert ToolRegistry().list_specs() == ()


def test_register_is_explicit_and_all_or_nothing(env: Env) -> None:
    registry = ToolRegistry()
    toolset = register_filesystem_write_tools(registry, [env.root])
    assert {s.name for s in registry.list_specs()} == {t.spec.name for t in toolset.tools}
    again = ToolRegistry()
    again.register(env.tools[FS_DELETE])
    with pytest.raises(DuplicateToolError):
        register_filesystem_write_tools(again, [env.root])
    assert {s.name for s in again.list_specs()} == {FS_DELETE}
    with pytest.raises(ValueError):
        register_filesystem_write_tools(ToolRegistry(), [])


def test_tools_do_not_expose_content_parameters(env: Env) -> None:
    for tool in env.tools.values():
        assert "content" not in tool.spec.input_schema["properties"]
        assert "text" not in tool.spec.input_schema["properties"]


# Approval flows -------------------------------------------------------------------------


def test_write_needs_approval_and_summary_hides_content(env: Env) -> None:
    args = env.write_args("note.md", SECRET_TEXT)
    first = run(env.executor.execute(env.call(FS_WRITE_FILE, args)))
    assert first.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert not (env.root_path / "note.md").exists()
    pending = env.store.list_pending()
    assert len(pending) == 1
    shown = repr(pending[0].summary)
    assert SECRET_TEXT not in shown
    assert pending[0].summary["digest_prefix"] == digest_arguments(args)[:12]
    with env.db.connect() as connection:
        dump = repr(connection.execute("SELECT * FROM tool_approvals").fetchall())
    assert SECRET_TEXT not in dump
    assert SECRET_TEXT not in repr(summarize_arguments(args))


def test_approved_write_runs_once_and_is_one_shot(env: Env) -> None:
    args = env.write_args("note.md", "hello")
    done = env.approved(FS_WRITE_FILE, args)
    assert done.result.status is ToolStatus.OK
    assert done.result.output["bytes"] == 5 and done.result.output["replaced"] is False
    assert (env.root_path / "note.md").read_text(encoding="utf-8") == "hello"
    # A repeat needs a fresh approval (and now would also hit "exists").
    args2 = env.write_args("note2.md", "hello")
    first = run(env.executor.execute(env.call(FS_WRITE_FILE, args2)))
    env.store.decide(first.approval_id, True)
    assert run(env.executor.execute(env.call(FS_WRITE_FILE, args2))).result.status is ToolStatus.OK
    replay = run(env.executor.execute(env.call(FS_WRITE_FILE, args2)))
    assert replay.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert replay.approval_id != first.approval_id


def test_approval_is_bound_to_path_content_hash_and_overwrite(env: Env) -> None:
    args = env.write_args("a.md", "one")
    first = run(env.executor.execute(env.call(FS_WRITE_FILE, args)))
    env.store.decide(first.approval_id, True)
    variants = [
        {**args, "path": "b.md"},
        {**args, **env.stage_text("two")},
        {**args, "overwrite": True},
        {**args, "root": "other"},
    ]
    for variant in variants:
        out = run(env.executor.execute(env.call(FS_WRITE_FILE, variant)))
        assert out.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert list(env.root_path.iterdir()) == [] and list(env.other_path.iterdir()) == []
    assert digest_arguments(args) != digest_arguments(variants[1])
    assert run(env.executor.execute(env.call(FS_WRITE_FILE, args))).result.status is ToolStatus.OK


def test_denied_and_expired_approvals_do_not_write(env: Env) -> None:
    args = env.write_args("a.md", "x")
    first = run(env.executor.execute(env.call(FS_WRITE_FILE, args)))
    env.store.decide(first.approval_id, False)
    out = run(env.executor.execute(env.call(FS_WRITE_FILE, args)))
    assert out.result.error is ToolErrorCode.CONFIRMATION_REQUIRED  # new pending request
    second = env.store.list_pending()[0]
    env.store.decide(second.id, True)
    env.clock.now += timedelta(seconds=120)
    out = run(env.executor.execute(env.call(FS_WRITE_FILE, args)))
    assert out.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert out.approval_state is ApprovalState.PENDING
    assert not (env.root_path / "a.md").exists()


@pytest.mark.parametrize("name", [FS_APPEND, FS_MAKE_DIR, FS_MOVE, FS_DELETE, FS_WRITE_FILE])
def test_every_tool_waits_for_a_human(env: Env, name: str) -> None:
    (env.root_path / "f.txt").write_text("x", encoding="utf-8")
    arguments = {
        FS_APPEND: {"root": "docs", "path": "f.txt", **env.stage_text("y")},
        FS_MAKE_DIR: {"root": "docs", "path": "d", "parents": False},
        FS_MOVE: {"root": "docs", "path": "f.txt", "to_path": "g.txt"},
        FS_DELETE: {"root": "docs", "path": "f.txt"},
        FS_WRITE_FILE: env.write_args("f.txt", "z", overwrite=True),
    }[name]
    out = run(env.executor.execute(env.call(name, arguments)))
    assert out.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert [p.name for p in env.root_path.iterdir()] == ["f.txt"]
    assert (env.root_path / "f.txt").read_text(encoding="utf-8") == "x"


def test_tool_refuses_to_run_unconfirmed_even_if_policy_allows_yellow(env: Env) -> None:
    (env.root_path / "f.txt").write_text("x", encoding="utf-8")
    policy = PermissionPolicy(
        allow_yellow=frozenset({FS_APPEND}), scope_checks=env.toolset.scope_checks
    )
    registry = ToolRegistry(policy, cancel_grace_seconds=0.1)
    registry.register(env.tools[FS_APPEND])
    args = {"root": "docs", "path": "f.txt", **env.stage_text("y")}
    result = run(registry.invoke(env.call(FS_APPEND, args)))
    assert result.status is not ToolStatus.OK
    assert (env.root_path / "f.txt").read_text(encoding="utf-8") == "x"
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_APPEND, args, confirmed=False)
    assert exc.value.code is ToolErrorCode.CONFIRMATION_REQUIRED


def test_denied_scope_never_reaches_the_approval_queue(env: Env) -> None:
    for path in (".env", "../x", "a.sqlite3", ".jarvis-trash/x"):
        args = env.write_args(path, "x")
        out = run(env.executor.execute(env.call(FS_WRITE_FILE, args)))
        assert out.result.status is ToolStatus.DENIED
        assert out.result.error is ToolErrorCode.PERMISSION_DENIED
    assert env.store.list_pending() == []


# Hostile paths --------------------------------------------------------------------------

HOSTILE = [
    "../escape.txt",
    "a/../../escape.txt",
    "/etc/passwd",
    "..",
    ".",
    "",
    "a//b.txt",
    "a\\b.txt",
    "C:evil.txt",
    "x\x00y.txt",
    "line\nbreak.txt",
    "dir/nul.txt",
    "trailing.",
    "trailing ",
    "a/./b.txt",
    "x" * 300,
    ".env",
    "sub/.ENV",
    ".Env.local",
    "id_rsa",
    "keys/server.PEM",
    ".git/config",
    ".ssh/authorized_keys",
    "app.sqlite3",
    "APP.DB",
    "x.db-wal",
    "Vault/note.md",
    "notes/VAULT.md".replace("VAULT.md", "vault"),
    "secrets.txt",
    "x/.obsidian/app.json",
    ".jarvis-trash/x",
    ".JARVIS-TRASH",
    ".jarvis-tmp-abc",
    unicodedata.normalize("NFD", "Ｖ").replace("Ｖ", "v") + "ault",
]


@pytest.mark.parametrize("path", HOSTILE)
def test_hostile_paths_are_refused_by_every_tool_and_scope_check(env: Env, path: str) -> None:
    (env.root_path / "sub").mkdir()
    text = env.stage_text("x")
    arg_sets = {
        FS_WRITE_FILE: {"root": "docs", "path": path, "overwrite": True, **text},
        FS_APPEND: {"root": "docs", "path": path, **text},
        FS_MAKE_DIR: {"root": "docs", "path": path, "parents": True},
        FS_MOVE: {"root": "docs", "path": path, "to_path": "moved"},
        FS_DELETE: {"root": "docs", "path": path},
    }
    for name, arguments in arg_sets.items():
        with pytest.raises(FilesystemToolError):
            env.direct(name, arguments)
        assert env.toolset.scope_checks[name](env.tools[name].spec, arguments) is False
    # and as the destination of a move
    (env.root_path / "src.txt").write_text("s", encoding="utf-8")
    with pytest.raises(FilesystemToolError):
        env.direct(FS_MOVE, {"root": "docs", "path": "src.txt", "to_path": path})
    assert sorted(p.name for p in env.root_path.iterdir()) == ["src.txt", "sub"]
    assert not (env.root_path.parent / "escape.txt").exists()
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


def test_unknown_root_and_missing_fields_are_refused(env: Env) -> None:
    text = env.stage_text("x")
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, {"root": "nope", "path": "a", "overwrite": False, **text})
    assert reason_of(exc) is FilesystemReason.UNKNOWN_ROOT
    with pytest.raises(FilesystemToolError):
        env.direct(FS_WRITE_FILE, {"root": "docs", "path": "a", **text})  # no overwrite flag
    with pytest.raises(FilesystemToolError):
        env.direct(FS_MAKE_DIR, {"root": "docs", "path": "a"})


def test_extra_denied_patterns_are_app_supplied(env: Env) -> None:
    toolset = filesystem_write_tools([env.root], stage=env.stage, extra_denied_patterns=["*.log"])
    write = toolset.tools[0]
    ctx = ToolContext("d", write.spec.name, PermissionLevel.RED, True, 5.0, CancellationToken())
    args = {"root": "docs", "path": "A.LOG", "overwrite": False, **env.stage_text("x")}
    with pytest.raises(FilesystemToolError):
        run(write.run(args, ctx))
    with pytest.raises(ValueError):
        filesystem_write_tools([env.root], extra_denied_patterns=["a/b"])


# write_file -----------------------------------------------------------------------------


def test_write_creates_private_file_atomically_without_temp_leftovers(env: Env) -> None:
    (env.root_path / "sub").mkdir()
    out = env.direct(FS_WRITE_FILE, env.write_args("sub/a.md", "héllo\n"))
    path = env.root_path / "sub" / "a.md"
    assert path.read_text(encoding="utf-8") == "héllo\n"
    assert out["sha256"] == hashlib.sha256("héllo\n".encode()).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert no_temp_files(env.root_path)


def test_write_refuses_to_clobber_without_overwrite(env: Env) -> None:
    (env.root_path / "a.md").write_text("old", encoding="utf-8")
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, env.write_args("a.md", "new"))
    assert reason_of(exc) is FilesystemReason.ALREADY_EXISTS
    assert (env.root_path / "a.md").read_text(encoding="utf-8") == "old"
    assert no_temp_files(env.root_path)


def test_overwrite_replaces_and_keeps_mode(env: Env) -> None:
    path = env.root_path / "a.md"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o640)
    out = env.direct(FS_WRITE_FILE, env.write_args("a.md", "new", overwrite=True))
    assert out["replaced"] is True
    assert path.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert no_temp_files(env.root_path)


def test_failed_write_leaves_original_and_no_temp(env: Env, monkeypatch) -> None:
    path = env.root_path / "a.md"
    path.write_text("old", encoding="utf-8")

    def boom(fd, data):
        os.write(fd, data[:1])
        raise OSError(28, "disk full")

    monkeypatch.setattr(fw, "_write_all", boom)
    with pytest.raises(FilesystemToolError):
        env.direct(FS_WRITE_FILE, env.write_args("a.md", "new", overwrite=True))
    assert path.read_text(encoding="utf-8") == "old"
    assert no_temp_files(env.root_path)


def test_missing_parent_directory_fails_and_creates_nothing(env: Env) -> None:
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, env.write_args("nope/a.md", "x"))
    assert reason_of(exc) is FilesystemReason.NOT_FOUND
    assert list(env.root_path.iterdir()) == []


def test_write_targets_that_are_not_regular_files(env: Env) -> None:
    (env.root_path / "d").mkdir()
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, env.write_args("d", "x", overwrite=True))
    assert reason_of(exc) is FilesystemReason.NOT_A_FILE
    os.mkfifo(env.root_path / "pipe")
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, env.write_args("pipe", "x", overwrite=True))
    assert reason_of(exc) is FilesystemReason.SPECIAL_FILE


def test_size_caps_and_staged_content_integrity(env: Env) -> None:
    small = WriteLimits(max_write_bytes=8)
    toolset = filesystem_write_tools([env.root], stage=env.stage, limits=small)
    write = toolset.tools[0]
    ctx = ToolContext("d", write.spec.name, PermissionLevel.RED, True, 5.0, CancellationToken())
    big = env.write_args("big.txt", "123456789")
    with pytest.raises(FilesystemToolError) as exc:
        run(write.run(big, ctx))
    assert reason_of(exc) is FilesystemReason.TOO_LARGE
    # Not staged at all.
    ghost = {
        "root": "docs",
        "path": "g.txt",
        "overwrite": False,
        "content_sha256": "0" * 64,
        "content_size": 1,
    }
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, ghost)
    assert reason_of(exc) is FilesystemReason.CONTENT_UNAVAILABLE
    # Wrong declared size for staged content.
    args = env.write_args("s.txt", "abc")
    args["content_size"] = 2
    with pytest.raises(FilesystemToolError):
        env.direct(FS_WRITE_FILE, args)
    # Tampered stage entry.
    digest = args["content_sha256"]
    env.stage._items[digest] = (b"evil", env.stage._items[digest][1])
    args["content_size"] = 4
    with pytest.raises(FilesystemToolError):
        env.direct(FS_WRITE_FILE, args)
    assert list(env.root_path.iterdir()) == []
    with pytest.raises(ValueError):
        WriteLimits(max_write_bytes=0)
    with pytest.raises(ValueError):
        WriteLimits(max_write_bytes=10**9)


def test_stage_is_bounded_expiring_and_text_only() -> None:
    now = [0.0]
    stage = ContentStage(max_entries=2, ttl_seconds=10, monotonic=lambda: now[0])
    a = stage.put("a")
    stage.put("b")
    with pytest.raises(ValueError):
        stage.put("c")
    assert stage.put("a") == a  # idempotent
    now[0] = 11.0
    assert len(stage) == 0 and stage.get(a.content_sha256, 1) is None
    with pytest.raises(ValueError):
        stage.put("nul\x00byte")
    with pytest.raises(TypeError):
        stage.put(b"bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        stage.put("\ud800")


def test_staged_content_is_discarded_after_success(env: Env) -> None:
    args = env.write_args("a.md", "once")
    env.direct(FS_WRITE_FILE, args)
    assert len(env.stage) == 0
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, {**args, "path": "again.md"})
    assert reason_of(exc) is FilesystemReason.CONTENT_UNAVAILABLE


# Symlinks and races ---------------------------------------------------------------------


def test_symlinked_directory_component_is_refused(env: Env) -> None:
    (env.root_path / "link").symlink_to(env.outside, target_is_directory=True)
    for name, arguments in {
        FS_WRITE_FILE: env.write_args("link/new.txt", "x"),
        FS_MAKE_DIR: {"root": "docs", "path": "link/newdir", "parents": True},
        FS_DELETE: {"root": "docs", "path": "link/victim.txt"},
        FS_MOVE: {"root": "docs", "path": "link/victim.txt", "to_path": "taken.txt"},
        FS_APPEND: {"root": "docs", "path": "link/victim.txt", **env.stage_text("x")},
    }.items():
        with pytest.raises(FilesystemToolError):
            env.direct(name, arguments)
    assert sorted(p.name for p in env.outside.iterdir()) == ["victim.txt"]
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


def test_symlinked_file_is_never_written_through(env: Env) -> None:
    (env.root_path / "l.txt").symlink_to(env.outside / "victim.txt")
    for overwrite in (True, False):
        with pytest.raises(FilesystemToolError):
            env.direct(FS_WRITE_FILE, env.write_args("l.txt", "x", overwrite=overwrite))
    with pytest.raises(FilesystemToolError):
        env.direct(FS_APPEND, {"root": "docs", "path": "l.txt", **env.stage_text("x")})
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE
    assert no_temp_files(env.root_path)


def test_dangling_symlink_target_is_not_created_through(env: Env) -> None:
    (env.root_path / "l.txt").symlink_to(env.outside / "new.txt")
    with pytest.raises(FilesystemToolError):
        env.direct(FS_WRITE_FILE, env.write_args("l.txt", "x"))
    assert not (env.outside / "new.txt").exists()


def test_symlink_swapped_in_after_check_is_replaced_not_followed(env: Env, monkeypatch) -> None:
    target = env.root_path / "a.md"
    target.write_text("old", encoding="utf-8")
    real = fw._write_all

    def swap(fd, data):
        real(fd, data)
        target.unlink()
        target.symlink_to(env.outside / "victim.txt")

    monkeypatch.setattr(fw, "_write_all", swap)
    env.direct(FS_WRITE_FILE, env.write_args("a.md", "new", overwrite=True))
    assert not target.is_symlink() and target.read_text(encoding="utf-8") == "new"
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


def test_file_created_by_a_racer_is_not_clobbered_without_overwrite(env: Env, monkeypatch) -> None:
    target = env.root_path / "a.md"
    real = fw._write_all

    def racer(fd, data):
        real(fd, data)
        target.write_text("racer", encoding="utf-8")

    monkeypatch.setattr(fw, "_write_all", racer)
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, env.write_args("a.md", "mine"))
    assert reason_of(exc) is FilesystemReason.ALREADY_EXISTS
    assert target.read_text(encoding="utf-8") == "racer" and no_temp_files(env.root_path)


def test_parent_swapped_to_symlink_after_realpath_check(env: Env, monkeypatch) -> None:
    sub = env.root_path / "sub"
    sub.mkdir()
    real = fsmod._check_realpath

    def swap(root, parts):
        real(root, parts)
        if parts == ("sub",):
            sub.rmdir()
            sub.symlink_to(env.outside, target_is_directory=True)

    monkeypatch.setattr(fsmod, "_check_realpath", swap)
    with pytest.raises(FilesystemToolError):
        env.direct(FS_WRITE_FILE, env.write_args("sub/new.txt", "x"))
    assert sorted(p.name for p in env.outside.iterdir()) == ["victim.txt"]


def test_append_target_swapped_to_symlink_between_check_and_open(env: Env, monkeypatch) -> None:
    target = env.root_path / "a.md"
    target.write_text("old", encoding="utf-8")
    real = fw._lstat_opt

    def swap(dir_fd, name):
        info = real(dir_fd, name)
        if name == "a.md" and not target.is_symlink():
            target.unlink()
            target.symlink_to(env.outside / "victim.txt")
        return info

    monkeypatch.setattr(fw, "_lstat_opt", swap)
    with pytest.raises(FilesystemToolError):
        env.direct(FS_APPEND, {"root": "docs", "path": "a.md", **env.stage_text("x")})
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


def test_root_replaced_by_another_directory_is_detected(env: Env) -> None:
    os.rename(env.root_path, env.root_path.parent / "moved")
    env.root_path.mkdir()
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_WRITE_FILE, env.write_args("a.md", "x"))
    assert reason_of(exc) is FilesystemReason.CHANGED
    assert list(env.root_path.iterdir()) == []


# append ---------------------------------------------------------------------------------


def test_append_adds_to_existing_file_only(env: Env) -> None:
    path = env.root_path / "log.md"
    path.write_text("a\n", encoding="utf-8")
    out = env.direct(FS_APPEND, {"root": "docs", "path": "log.md", **env.stage_text("b\n")})
    assert path.read_text(encoding="utf-8") == "a\nb\n" and out["file_size"] == 4
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_APPEND, {"root": "docs", "path": "none.md", **env.stage_text("c")})
    assert reason_of(exc) is FilesystemReason.NOT_FOUND
    assert not (env.root_path / "none.md").exists()


def test_append_respects_file_cap_and_refuses_hard_links(env: Env) -> None:
    toolset = filesystem_write_tools(
        [env.root], stage=env.stage, limits=WriteLimits(max_write_bytes=8, max_file_bytes=10)
    )
    append = toolset.tools[1]
    ctx = ToolContext("d", FS_APPEND, PermissionLevel.YELLOW, True, 5.0, CancellationToken())
    path = env.root_path / "f.txt"
    path.write_text("123456789", encoding="utf-8")
    with pytest.raises(FilesystemToolError) as exc:
        run(append.run({"root": "docs", "path": "f.txt", **env.stage_text("ab")}, ctx))
    assert reason_of(exc) is FilesystemReason.TOO_LARGE and path.read_text() == "123456789"
    os.link(env.outside / "victim.txt", env.root_path / "hard.txt")
    with pytest.raises(FilesystemToolError):
        env.direct(FS_APPEND, {"root": "docs", "path": "hard.txt", **env.stage_text("x")})
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


def test_overwrite_of_hard_link_breaks_the_link_instead_of_writing_through(env: Env) -> None:
    os.link(env.outside / "victim.txt", env.root_path / "hard.txt")
    env.direct(FS_WRITE_FILE, env.write_args("hard.txt", "new", overwrite=True))
    assert (env.root_path / "hard.txt").read_text(encoding="utf-8") == "new"
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


# make_dir -------------------------------------------------------------------------------


def test_make_dir_variants(env: Env) -> None:
    env.direct(FS_MAKE_DIR, {"root": "docs", "path": "a", "parents": False})
    assert (env.root_path / "a").is_dir()
    assert stat.S_IMODE((env.root_path / "a").stat().st_mode) == 0o700
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_MAKE_DIR, {"root": "docs", "path": "a", "parents": True})
    assert reason_of(exc) is FilesystemReason.ALREADY_EXISTS
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_MAKE_DIR, {"root": "docs", "path": "x/y", "parents": False})
    assert reason_of(exc) is FilesystemReason.NOT_FOUND
    assert not (env.root_path / "x").exists()
    env.direct(FS_MAKE_DIR, {"root": "docs", "path": "a/b/c", "parents": True})
    assert (env.root_path / "a" / "b" / "c").is_dir()
    deep = "/".join(f"d{i}" for i in range(12))
    with pytest.raises(FilesystemToolError):
        env.direct(FS_MAKE_DIR, {"root": "docs", "path": deep, "parents": True})
    assert (
        len([p for p in env.root_path.rglob("*") if p.name.startswith("d")]) <= fw.MAX_CREATED_DIRS
    )


def test_make_dir_over_existing_file_or_symlink_fails(env: Env) -> None:
    (env.root_path / "f").write_text("x", encoding="utf-8")
    (env.root_path / "l").symlink_to(env.outside, target_is_directory=True)
    for name in ("f", "l"):
        with pytest.raises(FilesystemToolError):
            env.direct(FS_MAKE_DIR, {"root": "docs", "path": name, "parents": True})
    with pytest.raises(FilesystemToolError):
        env.direct(FS_MAKE_DIR, {"root": "docs", "path": "f/sub", "parents": True})


# move -----------------------------------------------------------------------------------


def test_move_renames_files_and_directories(env: Env) -> None:
    (env.root_path / "a.txt").write_text("A", encoding="utf-8")
    (env.root_path / "d").mkdir()
    (env.root_path / "d" / "inner.txt").write_text("I", encoding="utf-8")
    (env.root_path / "dest").mkdir()
    env.direct(FS_MOVE, {"root": "docs", "path": "a.txt", "to_path": "dest/b.txt"})
    env.direct(FS_MOVE, {"root": "docs", "path": "d", "to_path": "dest/d2"})
    assert (env.root_path / "dest" / "b.txt").read_text(encoding="utf-8") == "A"
    assert (env.root_path / "dest" / "d2" / "inner.txt").read_text(encoding="utf-8") == "I"
    assert not (env.root_path / "a.txt").exists() and not (env.root_path / "d").exists()


def test_move_never_replaces_and_stays_in_one_root(env: Env) -> None:
    (env.root_path / "a.txt").write_text("A", encoding="utf-8")
    (env.root_path / "b.txt").write_text("B", encoding="utf-8")
    (env.root_path / "d").mkdir()
    (env.root_path / "e").mkdir()
    for src, dst in (("a.txt", "b.txt"), ("a.txt", "d"), ("d", "e"), ("d", "b.txt")):
        with pytest.raises(FilesystemToolError):
            env.direct(FS_MOVE, {"root": "docs", "path": src, "to_path": dst})
    assert (env.root_path / "a.txt").read_text() == "A" and (
        env.root_path / "b.txt"
    ).read_text() == "B"
    for src, dst in (("d", "d/sub"), ("d", "d"), ("d", "D/../d")):
        with pytest.raises(FilesystemToolError):
            env.direct(FS_MOVE, {"root": "docs", "path": src, "to_path": dst})
    with pytest.raises(FilesystemToolError):
        env.direct(FS_MOVE, {"root": "docs", "path": "missing", "to_path": "z"})
    assert list(env.other_path.iterdir()) == []


def test_move_of_a_symlink_moves_the_link_not_the_target(env: Env) -> None:
    (env.root_path / "l").symlink_to(env.outside / "victim.txt")
    env.direct(FS_MOVE, {"root": "docs", "path": "l", "to_path": "l2"})
    assert (env.root_path / "l2").is_symlink() and not os.path.lexists(env.root_path / "l")
    assert (env.outside / "victim.txt").read_text(encoding="utf-8") == OUTSIDE


def test_move_source_swapped_during_move_is_not_deleted(env: Env, monkeypatch) -> None:
    (env.root_path / "a.txt").write_text("A", encoding="utf-8")
    calls = {"n": 0}
    real = fw._lstat_opt

    def swap(dir_fd, name):
        info = real(dir_fd, name)
        calls["n"] += 1
        if calls["n"] == 1 and name == "a.txt":
            return info
        if calls["n"] == 2 and name == "a.txt":  # after the link was made
            (env.root_path / "a.txt").unlink()
            (env.root_path / "a.txt").write_text("NEW", encoding="utf-8")
            return real(dir_fd, name)
        return info

    monkeypatch.setattr(fw, "_lstat_opt", swap)
    with pytest.raises(FilesystemToolError) as exc:
        env.direct(FS_MOVE, {"root": "docs", "path": "a.txt", "to_path": "b.txt"})
    assert reason_of(exc) is FilesystemReason.CHANGED
    assert (env.root_path / "a.txt").read_text() == "NEW" and not (env.root_path / "b.txt").exists()


# delete (quarantine) --------------------------------------------------------------------


def test_delete_moves_into_trash_and_never_removes_data(env: Env) -> None:
    (env.root_path / "a.txt").write_text(SECRET_TEXT, encoding="utf-8")
    (env.root_path / "d").mkdir()
    (env.root_path / "d" / "inner.txt").write_text("I", encoding="utf-8")
    out = env.direct(FS_DELETE, {"root": "docs", "path": "a.txt"})
    env.direct(FS_DELETE, {"root": "docs", "path": "d"})
    assert not (env.root_path / "a.txt").exists() and not (env.root_path / "d").exists()
    trash = env.root_path / TRASH_DIR
    assert stat.S_IMODE(trash.stat().st_mode) == 0o700
    assert (env.root_path / out["trashed_to"]).read_text(encoding="utf-8") == SECRET_TEXT
    contents = sorted(p.name for p in trash.iterdir())
    assert len(contents) == 2 and contents[0].endswith("-a.txt") or contents[1].endswith("-a.txt")
    inner = [p for p in trash.rglob("inner.txt")]
    assert len(inner) == 1 and inner[0].read_text() == "I"


def test_delete_same_name_twice_keeps_both(env: Env) -> None:
    for text in ("one", "two"):
        (env.root_path / "a.txt").write_text(text, encoding="utf-8")
        env.direct(FS_DELETE, {"root": "docs", "path": "a.txt"})
    texts = sorted(p.read_text() for p in (env.root_path / TRASH_DIR).iterdir())
    assert texts == ["one", "two"]


def test_trash_cannot_be_addressed_by_any_write_tool(env: Env) -> None:
    (env.root_path / "a.txt").write_text("x", encoding="utf-8")
    out = env.direct(FS_DELETE, {"root": "docs", "path": "a.txt"})
    entry = out["trashed_to"]
    attempts = [
        (FS_DELETE, {"root": "docs", "path": entry}),
        (FS_DELETE, {"root": "docs", "path": TRASH_DIR}),
        (FS_MOVE, {"root": "docs", "path": entry, "to_path": "back.txt"}),
        (FS_WRITE_FILE, env.write_args(entry, "x", overwrite=True)),
    ]
    for name, arguments in attempts:
        with pytest.raises(FilesystemToolError):
            env.direct(name, arguments)
    assert (env.root_path / entry).read_text() == "x"


def test_symlinked_trash_is_refused(env: Env) -> None:
    (env.root_path / TRASH_DIR).symlink_to(env.outside, target_is_directory=True)
    (env.root_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FilesystemToolError):
        env.direct(FS_DELETE, {"root": "docs", "path": "a.txt"})
    assert (env.root_path / "a.txt").exists()
    assert sorted(p.name for p in env.outside.iterdir()) == ["victim.txt"]


def test_delete_missing_and_root_are_refused(env: Env) -> None:
    for path in ("missing.txt", "", "."):
        with pytest.raises(FilesystemToolError):
            env.direct(FS_DELETE, {"root": "docs", "path": path})
    assert not (env.root_path / TRASH_DIR / "x").exists()


def test_delete_with_very_long_unicode_name_fits(env: Env) -> None:
    name = "あ" * 80 + ".txt"  # 240 bytes: too long once the trash prefix is added
    (env.root_path / name).write_text("x", encoding="utf-8")
    env.direct(FS_DELETE, {"root": "docs", "path": name})
    assert not (env.root_path / name).exists()
    assert len(list((env.root_path / TRASH_DIR).iterdir())) == 1


# End to end through the approval executor ------------------------------------------------


def test_full_flow_write_append_move_delete_with_approvals(env: Env) -> None:
    (env.root_path / "notes").mkdir()
    assert (
        env.approved(FS_WRITE_FILE, env.write_args("notes/a.md", "A\n")).result.status
        is ToolStatus.OK
    )
    app = {"root": "docs", "path": "notes/a.md", **env.stage_text("B\n")}
    assert env.approved(FS_APPEND, app).result.status is ToolStatus.OK
    mv = {"root": "docs", "path": "notes/a.md", "to_path": "notes/b.md"}
    assert env.approved(FS_MOVE, mv).result.status is ToolStatus.OK
    assert (env.root_path / "notes" / "b.md").read_text(encoding="utf-8") == "A\nB\n"
    rm = {"root": "docs", "path": "notes/b.md"}
    out = env.approved(FS_DELETE, rm)
    assert out.result.status is ToolStatus.OK
    assert (env.root_path / out.result.output["trashed_to"]).read_text(encoding="utf-8") == "A\nB\n"
    assert env.store.list_pending() == []


def test_scope_checks_validate_content_fields(env: Env) -> None:
    check = env.toolset.scope_checks[FS_WRITE_FILE]
    spec = env.tools[FS_WRITE_FILE].spec
    good = env.write_args("a.md", "x")
    assert check(spec, good) is True
    assert check(spec, {**good, "content_sha256": "XYZ"}) is False
    assert check(spec, {**good, "content_size": -1}) is False
    assert check(spec, {**good, "content_size": True}) is False
    assert check(spec, {**good, "content_size": 10**9}) is False
    assert filesystem_write_scope_checks([env.root]).keys() == env.toolset.scope_checks.keys()
