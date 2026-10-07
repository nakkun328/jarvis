"""Shell tool: argument grammar, cwd/executable/env validation, execution, cleanup, wiring.

Only harmless programs run, always inside pytest's tmp_path: the current Python interpreter
executing tiny scripts, and /bin/echo. Fake sensitive environment values are built at runtime.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.tools import shell
from backend.tools.contract import (
    CancellationToken,
    PermissionLevel,
    ToolCall,
    ToolContext,
    ToolErrorCode,
    ToolResult,
    ToolStatus,
    canonical_json,
)
from backend.tools.permission import ConfirmationGrant, PermissionPolicy
from backend.tools.registry import InMemoryAuditSink, ToolRegistry
from backend.tools.shell import (
    AllowedCommand,
    ArgContext,
    ArgumentPolicy,
    CommandCatalog,
    FlagRule,
    InMemoryShellAuditSink,
    IntRange,
    Matches,
    OneOf,
    PlainText,
    RelPath,
    ShellConfigError,
    ShellFacts,
    ShellOutcome,
    ShellRejected,
    ShellRejection,
    ShellTool,
    ShellUnsupportedPlatformError,
    ValueStyle,
    VerificationOutcome,
    VerificationStatus,
    build_environment,
    build_shell_tools,
    git_readonly_command,
    has_unsafe_characters,
    pytest_command,
    sanitize_output,
    shell_scope_checks,
    verify_shell_result,
)

PYTHON = sys.executable
R = ShellRejection

# Helpers ---------------------------------------------------------------------------------------


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def script(work: Path, name: str, body: str) -> str:
    (work / name).write_text(textwrap.dedent(body))
    return name


def py_policy(**overrides) -> ArgumentPolicy:
    fields = {"positionals": RelPath(), "max_positionals": 4}
    fields.update(overrides)
    return ArgumentPolicy(**fields)


def py_command(work: Path, **overrides) -> AllowedCommand:
    fields = {
        "name": "py",
        "executable": PYTHON,
        "permission": PermissionLevel.YELLOW,
        "argument_policy": py_policy(),
        "cwd_roots": {"work": str(work)},
        "timeout_seconds": 10.0,
        "max_output_bytes": 8192,
    }
    fields.update(overrides)
    return AllowedCommand(**fields)


def make_tool(command: AllowedCommand, **kwargs) -> ShellTool:
    kwargs.setdefault("term_grace_seconds", 0.2)
    return ShellTool(CommandCatalog([command]), command.permission, **kwargs)


def request(command: str = "py", args=(), path: str = ".", root: str = "work") -> dict:
    return {"command": command, "args": list(args), "cwd": {"root": root, "path": path}}


def context(tool: ShellTool, *, confirmed: bool = False, token=None, call_id="c1") -> ToolContext:
    return ToolContext(
        call_id=call_id,
        tool_name=tool.spec.name,
        permission=tool.spec.permission,
        confirmed=confirmed,
        timeout_seconds=tool.spec.timeout_seconds,
        cancellation=token if token is not None else CancellationToken(),
    )


def run(tool: ShellTool, req: dict, **ctx):
    return asyncio.run(tool.run(req, context(tool, **ctx)))


def rejected_reason(tool: ShellTool, req: dict) -> ShellRejection:
    with pytest.raises(ShellRejected) as info:
        tool.check_request(req)
    return info.value.reason


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def assert_dead(*pids: int, limit: float = 3.0) -> None:
    deadline = time.monotonic() + limit
    while any(pid_alive(p) for p in pids):
        assert time.monotonic() < deadline, "process survived cleanup"
        time.sleep(0.02)


def read_pids(work: Path) -> list[int]:
    return [int(p) for p in (work / "pids.txt").read_text().split()]


async def wait_for_file(path: Path, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not path.exists():
        assert time.monotonic() < deadline, "child never started"
        await asyncio.sleep(0.02)


SPAWN_GRANDCHILD = """
    import os, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open("pids.tmp", "w") as fh:
        fh.write(f"{os.getpid()} {child.pid}")
    os.rename("pids.tmp", "pids.txt")
    time.sleep(60)
"""

# Registration: AllowedCommand ------------------------------------------------------------------


def test_command_resolves_executable_and_roots(tmp_path, work):
    link = tmp_path / "py-link"
    link.symlink_to(PYTHON)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(work)
    command = py_command(work, executable=str(link), cwd_roots={"work": str(root_link)})
    assert command.resolved_executable == os.path.realpath(PYTHON)
    assert command.cwd_roots["work"] == os.path.realpath(work)


@pytest.mark.parametrize("bad", ["python", "./python", "", "relative/bin/tool", "\x00/bin/sh"])
def test_relative_or_malformed_executable_rejected(work, bad):
    with pytest.raises(ShellConfigError):
        py_command(work, executable=bad)


def test_missing_directory_and_non_executable_rejected(tmp_path, work):
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    plain.chmod(0o644)
    for bad in (tmp_path / "nope", tmp_path, plain):
        with pytest.raises(ShellConfigError):
            py_command(work, executable=str(bad))


@pytest.mark.parametrize("bad", ["", "Py", "1py", "py command", "p" * 33, "py;ls", 5])
def test_command_names_are_short_labels(work, bad):
    with pytest.raises(ShellConfigError):
        py_command(work, name=bad)


def test_cwd_roots_validated(tmp_path, work):
    a_file = tmp_path / "file"
    a_file.write_text("x")
    for roots in (
        {},
        {"work": "relative/dir"},
        {"work": str(tmp_path / "missing")},
        {"work": str(a_file)},
        {"work": "/"},
        {"Bad Name": str(work)},
    ):
        with pytest.raises(ShellConfigError):
            py_command(work, cwd_roots=roots)


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": 601},
        {"timeout_seconds": True},
        {"max_output_bytes": 0},
        {"max_output_bytes": 65_537},
        {"max_output_bytes": 1.5},
        {"fixed_args": ("a\x00b",)},
        {"permission": "yellow"},
        {"argument_policy": None},
        {"cancellable": "yes"},
    ],
)
def test_numeric_and_type_bounds(work, overrides):
    with pytest.raises(ShellConfigError):
        py_command(work, **overrides)


def test_permission_rules(work):
    # Green needs read_only and a closed policy; Red cannot be read_only.
    closed = ArgumentPolicy(flags=(FlagRule(name="--version"),))
    ok = py_command(work, permission=PermissionLevel.GREEN, read_only=True, argument_policy=closed)
    assert ok.permission is PermissionLevel.GREEN
    with pytest.raises(ShellConfigError):
        py_command(work, permission=PermissionLevel.GREEN, argument_policy=closed)  # not read_only
    with pytest.raises(ShellConfigError):
        py_command(work, permission=PermissionLevel.GREEN, read_only=True)  # free positionals
    with pytest.raises(ShellConfigError):
        py_command(work, permission=PermissionLevel.RED, read_only=True)
    assert py_command(work, permission=PermissionLevel.RED).permission is PermissionLevel.RED


def test_env_allowlist_refuses_sensitive_and_dangerous_names(work):
    refused = [
        "OPENAI_API" + "_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET" + "_ACCESS_KEY",
        "MY_PASSWORD",
        "SESSION_ID",
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "GIT_EXTERNAL_DIFF",
        "BASH_ENV",
        "bad name",
        "1BAD",
        "",
    ]
    for name in refused:
        with pytest.raises(ShellConfigError):
            py_command(work, env_allowlist=(name,))
    assert py_command(work, env_allowlist=("TZ", "CI")).env_allowlist == ("TZ", "CI")


def test_env_fixed_rules(work):
    assert py_command(work, env_fixed={"GIT_PAGER": "cat"}).env_fixed["GIT_PAGER"] == "cat"
    for bad in ({"MY_TOKEN": "x"}, {"PATH": "/bin"}, {"X": "a\x00b"}, {"X": 5}, {"bad name": "x"}):
        with pytest.raises(ShellConfigError):
            py_command(work, env_fixed=bad)
    with pytest.raises(ShellConfigError):
        py_command(work, env_allowlist=("TZ",), env_fixed={"TZ": "UTC"})


def test_catalog_rejects_duplicates_and_lists_names(work):
    a, b = py_command(work, name="alpha"), py_command(work, name="beta")
    catalog = CommandCatalog([b, a])
    assert catalog.names() == ("alpha", "beta")
    assert catalog.get("alpha") is a
    assert catalog.get("missing") is None
    assert catalog.get(None) is None
    with pytest.raises(ShellConfigError):
        CommandCatalog([a, py_command(work, name="alpha")])
    with pytest.raises(ShellConfigError):
        CommandCatalog(["not a command"])


def test_windows_fails_closed(monkeypatch, work):
    command = py_command(work)
    catalog = CommandCatalog([command])
    monkeypatch.setattr(shell, "_posix_supported", lambda: False)
    with pytest.raises(ShellUnsupportedPlatformError):
        py_command(work)
    with pytest.raises(ShellUnsupportedPlatformError):
        CommandCatalog([command])
    with pytest.raises(ShellUnsupportedPlatformError):
        ShellTool(catalog, PermissionLevel.YELLOW)
    with pytest.raises(ShellUnsupportedPlatformError):
        build_shell_tools(catalog)


# Argument grammar ------------------------------------------------------------------------------

GRAMMAR = ArgumentPolicy(
    flags=(
        FlagRule(name="-v"),
        FlagRule(name="--level", value=IntRange(0, 9), style=ValueStyle.EITHER),
        FlagRule(name="--mode", value=OneOf(("fast", "slow"))),
        FlagRule(pattern=r"-[0-9]{1,2}"),
        FlagRule(name="--tag", value=Matches(r"[a-z]{1,8}"), style=ValueStyle.JOINED, max_uses=2),
    ),
    positionals=Matches(r"[A-Za-z0-9_.]{1,16}"),
    max_positionals=2,
    after_double_dash=PlainText(16),
    max_after_double_dash=2,
    forbidden=(r"--exec.*", r"-c.*"),
    max_args=8,
    max_arg_length=32,
)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["-v"],
        ["--level", "3"],
        ["--level=3"],
        ["--mode", "fast"],
        ["-12"],
        ["abc"],
        ["abc", "def.txt"],
        ["--tag=ab", "--tag=cd"],
        ["--", "-weird"],
        ["--", "a b", "c;d"],
        ["-v", "abc", "--", "x", "y"],
        ("-v", "abc"),
    ],
)
def test_grammar_accepts(args):
    assert GRAMMAR.parse(args, None) == tuple(args)


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["-x"], R.ARG_NOT_ALLOWED),
        (["-"], R.ARG_NOT_ALLOWED),
        (["--unknown"], R.ARG_NOT_ALLOWED),
        (["-123"], R.ARG_NOT_ALLOWED),
        (["--level"], R.ARG_MISSING_VALUE),
        (["--level", "10"], R.ARG_VALUE_INVALID),
        (["--level=abc"], R.ARG_VALUE_INVALID),
        (["--level", "-1"], R.ARG_VALUE_INVALID),
        (["--mode=fast"], R.ARG_NOT_ALLOWED),
        (["--mode", "medium"], R.ARG_VALUE_INVALID),
        (["--mode", "-v"], R.ARG_VALUE_INVALID),
        (["--tag", "ab"], R.ARG_NOT_ALLOWED),
        (["--tag=AB"], R.ARG_VALUE_INVALID),
        (["--tag=a", "--tag=b", "--tag=c"], R.ARG_REPEATED),
        (["-v", "-v"], R.ARG_REPEATED),
        (["a", "b", "c"], R.ARG_TOO_MANY_POSITIONALS),
        (["--", "a", "b", "c"], R.ARG_TOO_MANY_POSITIONALS),
        (["-c"], R.ARG_FORBIDDEN),
        (["-cfoo"], R.ARG_FORBIDDEN),
        (["--exec=ls"], R.ARG_FORBIDDEN),
        (["--exec", "ls"], R.ARG_FORBIDDEN),
        (["--execute"], R.ARG_FORBIDDEN),
        (["; rm -rf /"], R.ARG_VALUE_INVALID),
        (["a;b"], R.ARG_VALUE_INVALID),
        (["$(id)"], R.ARG_VALUE_INVALID),
        (["`id`"], R.ARG_VALUE_INVALID),
        (["a b"], R.ARG_VALUE_INVALID),
        (["a|b"], R.ARG_VALUE_INVALID),
        (["a&&b"], R.ARG_VALUE_INVALID),
        (["../x"], R.ARG_VALUE_INVALID),
        (["~"], R.ARG_VALUE_INVALID),
        (["a\nb"], R.ARG_CHARACTERS),
        (["a\rb"], R.ARG_CHARACTERS),
        (["a\tb"], R.ARG_CHARACTERS),
        (["a\x00b"], R.ARG_CHARACTERS),
        (["a\x1b[0m"], R.ARG_CHARACTERS),
        (["a\x7f"], R.ARG_CHARACTERS),
        (["a\u0085b"], R.ARG_CHARACTERS),
        (["a\u202eb"], R.ARG_CHARACTERS),
        (["\u200b"], R.ARG_CHARACTERS),
        (["a\u2028b"], R.ARG_CHARACTERS),
        (["a\ud800b"], R.ARG_CHARACTERS),
        (["\u00e9"], R.ARG_VALUE_INVALID),
        ([""], R.ARG_LENGTH),
        (["x" * 33], R.ARG_LENGTH),
        ([1], R.ARG_LENGTH),
        ([None], R.ARG_LENGTH),
        ([["nested"]], R.ARG_LENGTH),
        (["a"] * 9, R.ARG_COUNT),
        ("-v", R.INVALID_REQUEST),
        (None, R.INVALID_REQUEST),
        ({"-v": 1}, R.INVALID_REQUEST),
    ],
)
def test_grammar_rejects(args, reason):
    with pytest.raises(ShellRejected) as info:
        GRAMMAR.parse(args, None)
    assert info.value.reason is reason
    assert str(info.value) == reason.value  # fixed code only


def test_double_dash_needs_explicit_opt_in():
    plain = ArgumentPolicy(flags=(FlagRule(name="-v"),))
    with pytest.raises(ShellRejected) as info:
        plain.parse(["--"], None)
    assert info.value.reason is R.ARG_DOUBLE_DASH
    assert GRAMMAR.parse(["--"], None) == ("--",)


def test_empty_policy_accepts_only_no_arguments():
    empty = ArgumentPolicy()
    assert empty.parse([], None) == ()
    for args in (["a"], ["-v"], ["--"]):
        with pytest.raises(ShellRejected):
            empty.parse(args, None)


def test_subcommands_and_injected_arguments():
    policy = ArgumentPolicy(
        inject_args=("--root-flag",),
        subcommands={
            "show": ArgumentPolicy(
                flags=(FlagRule(name="--short"),), inject_args=("--safe",), max_args=3
            ),
            "list": ArgumentPolicy(),
        },
    )
    assert policy.parse(["show", "--short"], None) == ("--root-flag", "show", "--safe", "--short")
    assert policy.parse(["list"], None) == ("--root-flag", "list")
    for args, reason in (
        ([], R.ARG_SUBCOMMAND),
        (["--short"], R.ARG_SUBCOMMAND),
        (["SHOW"], R.ARG_SUBCOMMAND),
        (["show", "--long"], R.ARG_NOT_ALLOWED),
        (["list", "x"], R.ARG_TOO_MANY_POSITIONALS),
        (["show"] + ["--short"] * 4, R.ARG_COUNT),
        (["push"], R.ARG_SUBCOMMAND),
    ):
        with pytest.raises(ShellRejected) as info:
            policy.parse(args, None)
        assert info.value.reason is reason


def test_policy_definition_errors():
    bad_definitions = [
        lambda: ArgumentPolicy(positionals=RelPath()),  # validator without a count
        lambda: ArgumentPolicy(max_positionals=1),  # count without a validator
        lambda: ArgumentPolicy(max_args=0),
        lambda: ArgumentPolicy(max_args=65),
        lambda: ArgumentPolicy(max_arg_length=2000),
        lambda: ArgumentPolicy(forbidden=("(",)),
        lambda: ArgumentPolicy(flags=(FlagRule(name="-c"),), forbidden=(r"-c.*",)),
        lambda: ArgumentPolicy(flags=("-v",)),
        lambda: ArgumentPolicy(inject_args=("a\x00",)),
        lambda: ArgumentPolicy(
            flags=(FlagRule(name="-v"),), subcommands={"a": ArgumentPolicy()}
        ),  # flags + subcommands
        lambda: ArgumentPolicy(subcommands={"bad name": ArgumentPolicy()}),
        lambda: ArgumentPolicy(subcommands={"ok": "not a policy"}),
        lambda: FlagRule(),
        lambda: FlagRule(name="-a", pattern="-b"),
        lambda: FlagRule(name="v"),
        lambda: FlagRule(name="--"),
        lambda: FlagRule(name="--a=b"),
        lambda: FlagRule(pattern="v"),
        lambda: FlagRule(pattern="-("),
        lambda: FlagRule(name="-a", max_uses=0),
        lambda: Matches("("),
        lambda: Matches(""),
        lambda: OneOf(()),
        lambda: IntRange(5, 1),
        lambda: PlainText(max_length=0),
    ]
    for build in bad_definitions:
        with pytest.raises(ShellConfigError):
            build()
    deep = ArgumentPolicy()
    for _ in range(shell.MAX_SUBCOMMAND_DEPTH - 1):  # the deepest allowed policy
        deep = ArgumentPolicy(subcommands={"x": deep})
    with pytest.raises(ShellConfigError):
        ArgumentPolicy(subcommands={"x": deep})


def test_is_closed_tracks_free_form_arguments():
    assert ArgumentPolicy().is_closed()
    assert ArgumentPolicy(flags=(FlagRule(name="-v", value=IntRange(0, 3)),)).is_closed()
    assert not ArgumentPolicy(positionals=PlainText(), max_positionals=1).is_closed()
    assert not ArgumentPolicy(flags=(FlagRule(name="-f", value=RelPath()),)).is_closed()
    assert not ArgumentPolicy(after_double_dash=RelPath(), max_after_double_dash=1).is_closed()
    nested = ArgumentPolicy(
        subcommands={"a": ArgumentPolicy(positionals=PlainText(), max_positionals=1)}
    )
    assert not nested.is_closed()


def test_validator_errors_fail_closed():
    class Exploding:
        def accepts(self, value, context):
            raise RuntimeError("boom")

    policy = ArgumentPolicy(positionals=Exploding(), max_positionals=1)
    with pytest.raises(ShellRejected) as info:
        policy.parse(["a"], None)
    assert info.value.reason is R.ARG_VALUE_INVALID


def test_relpath_validator(tmp_path, work):
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "inner").mkdir()
    (work / "escape").symlink_to(outside)
    (work / "alias").symlink_to(work / "inner")
    (work / "ok.py").write_text("")
    cwd = os.path.realpath(work)
    validator = RelPath()

    def check(value, *, after=False, cwd_value=cwd):
        return validator.accepts(value, ArgContext(cwd_value, after))

    assert check("ok.py") and check("inner") and check("inner/new.py") and check(".")
    assert check("alias") and check("tests/test_x.py::test_y[1]")  # nodeids need not exist
    assert not check("../outside") and not check("inner/../ok.py") and not check("..")
    assert not check("/etc/passwd") and not check(str(outside))
    assert not check("escape") and not check("escape/file")  # symlink leaving the cwd
    assert not check("-rf") and check("-rf", after=True)  # leading '-' only after `--`
    assert not check("a\x00b")
    assert not check("ok.py", cwd_value=None)


def test_text_validators():
    ctx = ArgContext(None)
    assert Matches(r"[a-z]+").accepts("abc", ctx) and not Matches(r"[a-z]+").accepts("ab1", ctx)
    assert not Matches(r"[a-z-]+").accepts("-a", ctx)
    assert Matches(r"[a-z-]+", allow_leading_dash=True).accepts("-a", ctx)
    assert Matches(r"[a-z-]+").accepts("-a", ArgContext(None, after_double_dash=True))
    assert not Matches(r"\d+").accepts("\u0663", ctx)  # ASCII-only classes
    assert IntRange(1, 5).accepts("5", ctx) and not IntRange(1, 5).accepts("6", ctx)
    assert not IntRange(0, 5).accepts("+1", ctx) and not IntRange(0, 5).accepts("1.0", ctx)
    assert not IntRange(0, 5).accepts("0" * 11, ctx)
    assert PlainText(4).accepts("abcd", ctx) and not PlainText(4).accepts("abcde", ctx)
    assert not PlainText().accepts("a\x1bb", ctx) and not PlainText().accepts("-x", ctx)
    assert OneOf(("a",)).accepts("a", ctx) and not OneOf(("a",)).accepts("A", ctx)


def test_has_unsafe_characters():
    assert not has_unsafe_characters("plain text, \u00e9\u3042 and spaces")
    for bad in ("a\x00", "\n", "\x1b", "\x7f", "\u0085", "\u202e", "\u200b", "\u2028", "\ud800"):
        assert has_unsafe_characters(bad)


# Output text -----------------------------------------------------------------------------------


def test_sanitize_output_is_inert_and_not_longer_than_input():
    raw = b"ok\xff\xfe \x1b[31mred\x00\x07\r\n\ttab \xc2\x85 \xe2\x80\xae end"
    text = sanitize_output(raw)
    assert "\x1b" not in text and "\x00" not in text and "\x07" not in text and "\r" not in text
    assert "\u202e" not in text and "\u0085" not in text
    assert text.startswith("ok\ufffd\ufffd \u241b[31mred\u2400\u2407\u240d\n\ttab")
    assert len(text) <= len(raw)
    assert sanitize_output(b"") == ""
    assert sanitize_output("h\u00e9llo".encode()) == "h\u00e9llo"


# cwd validation --------------------------------------------------------------------------------


def test_cwd_resolution_and_rejections(tmp_path, work):
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "sub").mkdir()
    (work / "file.txt").write_text("x")
    (work / "escape").symlink_to(outside)
    (work / "alias").symlink_to(work / "sub")
    tool = make_tool(py_command(work))
    prepared = tool._prepare(request(path="sub"))
    assert prepared.cwd == os.path.realpath(work / "sub")
    assert tool._prepare(request(path="alias")).cwd == os.path.realpath(work / "sub")
    assert tool._prepare(request(path="")).cwd == os.path.realpath(work)
    assert tool._prepare({"command": "py", "cwd": {"root": "work"}}).cwd == os.path.realpath(work)

    cases = [
        (request(root="other"), R.UNKNOWN_CWD_ROOT),
        (request(root="Work"), R.UNKNOWN_CWD_ROOT),
        (request(path="../outside"), R.CWD_INVALID_PATH),
        (request(path="sub/../.."), R.CWD_INVALID_PATH),
        (request(path=str(outside)), R.CWD_INVALID_PATH),
        (request(path="/"), R.CWD_INVALID_PATH),
        (request(path="a\x00b"), R.CWD_INVALID_PATH),
        (request(path="a\nb"), R.CWD_INVALID_PATH),
        (request(path="x" * 600), R.CWD_INVALID_PATH),
        (request(path="escape"), R.CWD_ESCAPES_ROOT),
        (request(path="escape/deeper"), R.CWD_ESCAPES_ROOT),
        (request(path="missing"), R.CWD_NOT_FOUND),
        (request(path="file.txt"), R.CWD_NOT_DIRECTORY),
        ({"command": "py", "args": [], "cwd": {"root": "work", "extra": 1}}, R.INVALID_REQUEST),
        ({"command": "py", "args": [], "cwd": {"root": 5}}, R.INVALID_REQUEST),
        ({"command": "py", "args": [], "cwd": {"root": "work", "path": 5}}, R.INVALID_REQUEST),
        ({"command": "py", "args": [], "cwd": "work"}, R.INVALID_REQUEST),
        ({"command": "py", "args": []}, R.INVALID_REQUEST),
        ({"command": 5, "args": [], "cwd": {"root": "work"}}, R.INVALID_REQUEST),
        ({**request(), "executable": "/bin/sh"}, R.INVALID_REQUEST),  # model cannot name a program
        ("not a mapping", R.INVALID_REQUEST),
        (None, R.INVALID_REQUEST),
        (request(command="nope"), R.UNKNOWN_COMMAND),
        (request(command="PY"), R.UNKNOWN_COMMAND),
        (request(command="/bin/sh"), R.UNKNOWN_COMMAND),
        (request(command="py; ls"), R.UNKNOWN_COMMAND),
    ]
    for req, reason in cases:
        assert rejected_reason(tool, req) is reason, req


def test_cwd_root_swapped_after_registration_is_refused(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "root"
    link.symlink_to(first)
    tool = make_tool(py_command(first, cwd_roots={"work": str(link)}))
    tool.check_request(request())
    link.unlink()
    link.symlink_to(second)
    assert rejected_reason(tool, request()) is R.CWD_ROOT_CHANGED


# Executable re-validation ----------------------------------------------------------------------


def make_script_executable(tmp_path: Path, name: str = "prog.sh", body: str = "echo hi") -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def test_executable_changes_are_detected(tmp_path, work):
    prog = make_script_executable(tmp_path)
    tool = make_tool(py_command(work, executable=str(prog), argument_policy=ArgumentPolicy()))
    tool.check_request(request())
    assert run(tool, request())["stdout"] == "hi\n"

    prog.chmod(0o644)
    assert rejected_reason(tool, request()) is R.EXECUTABLE_MISSING
    prog.chmod(0o755)
    tool.check_request(request())

    replacement = make_script_executable(tmp_path, "replacement.sh", "echo evil")
    os.replace(replacement, prog)  # same path, different file
    assert rejected_reason(tool, request()) is R.EXECUTABLE_CHANGED

    prog.unlink()
    assert rejected_reason(tool, request()) is R.EXECUTABLE_MISSING


def test_symlink_swap_is_detected(tmp_path, work):
    first = make_script_executable(tmp_path, "first.sh", "echo first")
    second = make_script_executable(tmp_path, "second.sh", "echo second")
    link = tmp_path / "link"
    link.symlink_to(first)
    tool = make_tool(py_command(work, executable=str(link), argument_policy=ArgumentPolicy()))
    assert run(tool, request())["stdout"] == "first\n"
    link.unlink()
    link.symlink_to(second)
    assert rejected_reason(tool, request()) is R.EXECUTABLE_CHANGED
    with pytest.raises(ShellRejected):
        run(tool, request())  # the second gate in run() refuses as well


# Environment -----------------------------------------------------------------------------------

DUMP_ENV = """
    import json, os
    print(json.dumps(dict(os.environ)))
"""


def test_child_environment_is_scrubbed(monkeypatch, work):
    names = [
        "OPENAI_API" + "_KEY",
        "GITHUB_" + "TOKEN",
        "AWS_SECRET" + "_ACCESS_KEY",
        "ANTHROPIC_" + "API_KEY",
        "MY_SERVICE_PASSWORD",
        "SSH_AUTH_SOCK",
        "PYTHONPATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_SSH_COMMAND",
        "PYTEST_ADDOPTS",
        "JARVIS_TEST_HIDDEN",
    ]
    values = {name: f"value-{hashlib.sha256(name.encode()).hexdigest()[:12]}" for name in names}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("JARVIS_TEST_VISIBLE", "visible-value")
    monkeypatch.setenv("JARVIS_TEST_MISSING_IN_PARENT_ONLY", "x")
    monkeypatch.delenv("JARVIS_TEST_ABSENT", raising=False)

    name = script(work, "env.py", DUMP_ENV)
    tool = make_tool(
        py_command(
            work,
            env_allowlist=("JARVIS_TEST_VISIBLE", "JARVIS_TEST_ABSENT"),
            env_fixed={"JARVIS_TEST_FIXED": "fixed"},
        )
    )
    result = run(tool, request(args=[name]))
    assert result["exit_code"] == 0
    env = json.loads(result["stdout"])
    text = result["stdout"]
    for var, value in values.items():
        assert var not in env and value not in text
    assert "JARVIS_TEST_MISSING_IN_PARENT_ONLY" not in env
    assert env["JARVIS_TEST_VISIBLE"] == "visible-value"
    assert "JARVIS_TEST_ABSENT" not in env
    assert env["JARVIS_TEST_FIXED"] == "fixed"
    assert env["PATH"] == shell.SAFE_PATH
    home = env["HOME"]
    assert home != os.environ.get("HOME")
    assert os.path.basename(home).startswith("jarvis-shell-")
    for xdg in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR"):
        assert env[xdg].startswith(home + os.sep)
    assert not os.path.exists(home)  # the per-run directory is removed afterwards
    extras = set(env) - {"JARVIS_TEST_VISIBLE", "JARVIS_TEST_FIXED", "PATH", "HOME", "TMPDIR"}
    extras -= {"XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"}
    assert extras <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}  # added by the interpreter itself


def test_build_environment_uses_injected_parent(work, tmp_path):
    command = py_command(work, env_allowlist=("TZ",), env_fixed={"FIXED": "1"})
    scratch = str(tmp_path / "scratch")
    env = build_environment(command, {"TZ": "UTC", "OTHER": "no", "HOME": "/root"}, scratch)
    assert env["TZ"] == "UTC" and env["FIXED"] == "1" and "OTHER" not in env
    assert env["HOME"] == scratch and env["PATH"] == shell.SAFE_PATH
    assert "TZ" not in build_environment(command, {"TZ": "a\x00b"}, scratch)
    assert "TZ" not in build_environment(command, {"TZ": "x" * 5000}, scratch)


def test_parent_environment_can_be_injected(monkeypatch, work):
    monkeypatch.setenv("JARVIS_TEST_VISIBLE", "from-process")
    name = script(work, "env.py", DUMP_ENV)
    tool = make_tool(
        py_command(work, env_allowlist=("JARVIS_TEST_VISIBLE",)),
        parent_environ={"JARVIS_TEST_VISIBLE": "from-injected"},
    )
    assert "from-injected" in run(tool, request(args=[name]))["stdout"]


# Execution -------------------------------------------------------------------------------------


def test_runs_with_argv_and_cwd_and_no_shell(work):
    (work / "sub").mkdir()
    name = script(
        work,
        "show.py",
        """
        import os, sys
        print(repr(sys.argv[1:]))
        print(os.getcwd())
        print(os.getpgrp() == os.getpid(), os.getsid(0) == os.getpid())
        """,
    )
    (work / "sub" / "show.py").write_text((work / name).read_text())
    tool = make_tool(py_command(work))
    result = run(tool, request(args=["show.py", "arg-one"], path="sub"))
    lines = result["stdout"].splitlines()
    assert lines[0] == "['arg-one']"
    assert lines[1] == os.path.realpath(work / "sub")
    assert lines[2] == "True True"  # own session and process group
    assert result["exit_code"] == 0 and result["signal"] == 0 and result["signal_name"] == ""
    assert not result["timed_out"] and not result["cancelled"] and not result["truncated"]
    assert result["cleanup_complete"] is True
    assert result["command"] == "py" and result["duration_ms"] >= 0


def test_shell_metacharacters_stay_inert(work):
    marker = work / "pwned"
    name = script(work, "argv.py", "import sys\nprint(repr(sys.argv[1:]))\n")
    policy = ArgumentPolicy(positionals=PlainText(64, allow_leading_dash=False), max_positionals=12)
    tool = make_tool(py_command(work, argument_policy=policy))
    injections = [
        "; touch pwned",
        "$(touch pwned)",
        "`touch pwned`",
        "a && touch pwned",
        "a | touch pwned",
        "*",
        "~",
        "$HOME",
        "${IFS}",
    ]
    result = run(tool, request(args=[name, *injections]))
    assert not marker.exists()
    assert repr(injections) in result["stdout"]  # delivered as literal argv elements


def test_stdin_is_closed(work):
    name = script(work, "stdin.py", "import sys\nprint(repr(sys.stdin.read()))\n")
    result = run(make_tool(py_command(work)), request(args=[name]))
    assert result["stdout"].strip() == "''" and result["exit_code"] == 0


def test_nonzero_exit_is_a_fact_not_an_error(work):
    name = script(work, "fail.py", "import sys\nsys.stderr.write('bad\\n')\nsys.exit(3)\n")
    audit = InMemoryShellAuditSink()
    tool = make_tool(py_command(work), audit=audit)
    result = run(tool, request(args=[name]))
    assert (result["exit_code"], result["signal"]) == (3, 0)
    assert result["stderr"] == "bad\n" and result["stdout"] == ""
    assert not result["timed_out"] and not result["cancelled"]
    assert audit.records[-1].outcome is ShellOutcome.NONZERO_EXIT
    assert audit.records[-1].exit_code == 3
    assert not ShellFacts.from_output(result).exit_zero


def test_signal_death_is_reported(work):
    name = script(work, "die.py", "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
    audit = InMemoryShellAuditSink()
    result = run(make_tool(py_command(work), audit=audit), request(args=[name]))
    assert (result["exit_code"], result["signal"], result["signal_name"]) == (-1, 15, "SIGTERM")
    assert audit.records[-1].outcome is ShellOutcome.SIGNALED
    assert not result["timed_out"]


def test_output_is_decoded_and_neutralised(work):
    name = script(
        work,
        "raw.py",
        r"""
        import sys
        sys.stdout.buffer.write(b"ok\xff\xfe \x1b[31mred\x00\x07\n")
        sys.stderr.buffer.write(b"err \xc2\x85\r\n")
        """,
    )
    result = run(make_tool(py_command(work)), request(args=[name]))
    assert "\ufffd" in result["stdout"] and "\u241b[31mred" in result["stdout"]
    for text in (result["stdout"], result["stderr"]):
        assert not has_unsafe_characters(text.replace("\n", ""))
    assert result["stdout_bytes"] == len(b"ok\xff\xfe \x1b[31mred\x00\x07\n")


def test_output_cap_truncates_and_stops_the_child(work):
    name = script(
        work,
        "flood.py",
        """
        import sys
        while True:
            sys.stdout.write("x" * 1000)
            sys.stdout.flush()
        """,
    )
    audit = InMemoryShellAuditSink()
    tool = make_tool(py_command(work, max_output_bytes=4096, timeout_seconds=20), audit=audit)
    started = time.monotonic()
    result = run(tool, request(args=[name]))
    assert time.monotonic() - started < 10  # killed on the cap, not by the timeout
    assert result["truncated"] is True and result["timed_out"] is False
    assert result["stdout_bytes"] + result["stderr_bytes"] == 4096
    assert len(result["stdout"]) == 4096 and set(result["stdout"]) == {"x"}
    assert result["signal"] in (signal.SIGTERM, signal.SIGKILL)
    assert result["cleanup_complete"] is True
    assert audit.records[-1].outcome is ShellOutcome.OUTPUT_CAP and audit.records[-1].truncated


def test_output_exactly_at_the_cap_is_not_truncated(work):
    name = script(work, "exact.py", "import sys\nsys.stdout.write('a' * 100)\n")
    result = run(make_tool(py_command(work, max_output_bytes=100)), request(args=[name]))
    assert result["truncated"] is False and result["stdout_bytes"] == 100
    over = script(work, "over.py", "import sys\nsys.stdout.write('a' * 101)\n")
    result = run(make_tool(py_command(work, max_output_bytes=100)), request(args=[over]))
    assert result["truncated"] is True and result["stdout_bytes"] == 100


def test_cap_is_shared_by_stdout_and_stderr(work):
    name = script(
        work,
        "both.py",
        """
        import sys, time
        sys.stdout.write("o" * 60); sys.stdout.flush()
        time.sleep(0.2)
        sys.stderr.write("e" * 60); sys.stderr.flush()
        """,
    )
    result = run(make_tool(py_command(work, max_output_bytes=100)), request(args=[name]))
    assert result["truncated"] is True
    assert result["stdout_bytes"] + result["stderr_bytes"] == 100
    assert result["stdout_bytes"] == 60 and result["stderr_bytes"] == 40


def test_echo_without_python(work):
    echo = "/bin/echo"
    if not os.access(echo, os.X_OK):
        pytest.skip("/bin/echo not available")
    policy = ArgumentPolicy(positionals=PlainText(32), max_positionals=3)
    tool = make_tool(py_command(work, name="echo", executable=echo, argument_policy=policy))
    result = run(tool, request(command="echo", args=["hello", "w\u00f6rld"]))
    assert result["stdout"] == "hello w\u00f6rld\n" and result["exit_code"] == 0
    assert rejected_reason(tool, request(command="echo", args=["-n"])) is R.ARG_NOT_ALLOWED


# Timeouts, cancellation, process cleanup ---------------------------------------------------------


def test_timeout_kills_the_whole_process_group(work):
    name = script(work, "tree.py", SPAWN_GRANDCHILD)
    audit = InMemoryShellAuditSink()
    tool = make_tool(py_command(work, timeout_seconds=1.0), audit=audit)
    started = time.monotonic()
    result = run(tool, request(args=[name]))
    assert time.monotonic() - started < 8
    assert result["timed_out"] is True and result["cancelled"] is False
    assert result["exit_code"] == -1 and result["signal"] in (signal.SIGTERM, signal.SIGKILL)
    assert result["cleanup_complete"] is True
    leader, grandchild = read_pids(work)
    assert_dead(leader, grandchild)
    assert audit.records[-1].outcome is ShellOutcome.TIMED_OUT


def test_sigterm_is_followed_by_sigkill(work):
    name = script(
        work,
        "stubborn.py",
        """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        open("ready.txt", "w").write("1")
        time.sleep(60)
        """,
    )
    tool = make_tool(py_command(work, timeout_seconds=1.0))
    result = run(tool, request(args=[name]))
    assert result["timed_out"] is True and result["signal"] == signal.SIGKILL
    assert result["cleanup_complete"] is True


def test_background_processes_do_not_outlive_a_normal_exit(work):
    name = script(
        work,
        "daemonish.py",
        """
        import os, subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open("pids.txt", "w").write(f"{os.getpid()} {child.pid}")
        print("parent done")
        """,
    )
    started = time.monotonic()
    result = run(make_tool(py_command(work, timeout_seconds=20)), request(args=[name]))
    assert time.monotonic() - started < 8  # the straggler did not hold the pipes open
    assert result["exit_code"] == 0 and result["stdout"] == "parent done\n"
    assert result["cleanup_complete"] is True
    assert_dead(*read_pids(work))


def test_cancellation_token_stops_the_group(work):
    name = script(work, "tree.py", SPAWN_GRANDCHILD)
    audit = InMemoryShellAuditSink()
    tool = make_tool(py_command(work, timeout_seconds=30), audit=audit)

    async def scenario():
        token = CancellationToken()
        task = asyncio.ensure_future(tool.run(request(args=[name]), context(tool, token=token)))
        await wait_for_file(work / "pids.txt")
        token.cancel()
        return await asyncio.wait_for(task, 10)

    result = asyncio.run(scenario())
    assert result["cancelled"] is True and result["timed_out"] is False
    assert result["cleanup_complete"] is True
    assert_dead(*read_pids(work))
    assert audit.records[-1].outcome is ShellOutcome.CANCELLED


def test_cancelling_the_awaiting_task_kills_the_group_and_is_audited(work):
    name = script(work, "tree.py", SPAWN_GRANDCHILD)
    audit = InMemoryShellAuditSink()
    tool = make_tool(py_command(work, timeout_seconds=30), audit=audit)

    async def scenario():
        task = asyncio.ensure_future(tool.run(request(args=[name]), context(tool)))
        await wait_for_file(work / "pids.txt")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert_dead(*read_pids(work))
    assert audit.records[-1].outcome is ShellOutcome.CANCELLED


def test_non_cancellable_command_ignores_the_token(work):
    name = script(work, "slow.py", "import time\ntime.sleep(0.5)\nprint('finished')\n")
    tool = make_tool(py_command(work, cancellable=False))
    assert tool.spec.cancellable is False
    token = CancellationToken()
    token.cancel()
    result = asyncio.run(tool.run(request(args=[name]), context(tool, token=token)))
    assert result["cancelled"] is False and result["stdout"] == "finished\n"


def test_concurrent_runs_are_isolated(work):
    name = script(
        work,
        "who.py",
        """
        import os, sys, time
        time.sleep(0.2)
        print(sys.argv[1], os.environ["HOME"], os.getpid())
        """,
    )
    tool = make_tool(py_command(work))

    async def scenario():
        runs = [
            tool.run(request(args=[name, str(i)]), context(tool, call_id=f"c{i}")) for i in range(6)
        ]
        return await asyncio.gather(*runs)

    results = asyncio.run(scenario())
    parts = [r["stdout"].split() for r in results]
    assert [p[0] for p in parts] == [str(i) for i in range(6)]
    assert len({p[1] for p in parts}) == 6 and len({p[2] for p in parts}) == 6
    assert all(r["exit_code"] == 0 and r["cleanup_complete"] for r in results)
    assert not any(os.path.exists(p[1]) for p in parts)


def test_spawn_failure_is_a_fixed_reason(tmp_path, work):
    prog = tmp_path / "bad-format"
    prog.write_bytes(b"\x00\x01 not a real executable")
    prog.chmod(0o755)
    audit = InMemoryShellAuditSink()
    tool = make_tool(
        py_command(work, executable=str(prog), argument_policy=ArgumentPolicy()), audit=audit
    )
    with pytest.raises(ShellRejected) as info:
        run(tool, request())
    assert info.value.reason is R.SPAWN_FAILED
    assert audit.records[-1].outcome is ShellOutcome.SPAWN_FAILED
    assert str(info.value) == "spawn_failed"


# Tool spec and wiring --------------------------------------------------------------------------


def test_tools_are_split_by_permission_level(work):
    closed = ArgumentPolicy(flags=(FlagRule(name="--version"),))
    catalog = CommandCatalog(
        [
            py_command(work, name="green", permission=PermissionLevel.GREEN, read_only=True,
                       argument_policy=closed),
            py_command(work, name="yellow", timeout_seconds=7),
            py_command(work, name="red", permission=PermissionLevel.RED, timeout_seconds=9,
                       cancellable=False),
        ]
    )  # fmt: skip
    tools = build_shell_tools(catalog)
    by_level = {t.spec.permission: t for t in tools}
    assert by_level[PermissionLevel.GREEN].spec.name == "shell.run_readonly"
    assert by_level[PermissionLevel.YELLOW].spec.name == "shell.run"
    assert by_level[PermissionLevel.RED].spec.name == "shell.run_confirmed"
    yellow = by_level[PermissionLevel.YELLOW]
    assert yellow.command_names == ("yellow",)
    assert yellow.spec.timeout_seconds == 7 + shell.SPEC_TIMEOUT_MARGIN_SECONDS
    assert by_level[PermissionLevel.RED].spec.cancellable is False
    assert yellow.spec.cancellable is True
    assert PYTHON not in yellow.spec.description and str(work) not in yellow.spec.description
    assert "yellow" in yellow.spec.description
    assert set(shell_scope_checks(tools)) == {t.spec.name for t in tools}
    only_yellow = build_shell_tools(CommandCatalog([py_command(work)]))
    assert [t.spec.name for t in only_yellow] == ["shell.run"]
    with pytest.raises(ShellConfigError):
        ShellTool(CommandCatalog([py_command(work)]), PermissionLevel.RED)
    with pytest.raises(ShellConfigError):
        ShellTool(CommandCatalog([py_command(work)]), PermissionLevel.YELLOW, term_grace_seconds=99)


def test_scope_check_never_raises_and_fails_closed(work):
    tool = make_tool(py_command(work))
    spec = tool.spec
    assert tool.scope_check(spec, request(args=["a.py"])) is True
    for bad in (None, 5, "x", [], {"command": "py"}, request(command="zz"), request(root="zz")):
        assert tool.scope_check(spec, bad) is False


class Rig:
    """Registry + tools + sinks for the integration tests."""

    def __init__(self, work, *commands, allow_yellow=(), scope=True, clock=None, **tool_kwargs):
        self.shell_audit = InMemoryShellAuditSink()
        self.audit = InMemoryAuditSink()
        catalog = CommandCatalog(commands or [py_command(work)])
        tool_kwargs.setdefault("term_grace_seconds", 0.2)
        self.tools = build_shell_tools(catalog, audit=self.shell_audit, **tool_kwargs)
        kwargs = {"allow_yellow": frozenset(allow_yellow)}
        if scope:
            kwargs["scope_checks"] = shell_scope_checks(self.tools)
        if clock is not None:
            kwargs["clock"] = clock
        self.policy = PermissionPolicy(**kwargs)
        self.registry = ToolRegistry(self.policy, audit=self.audit)
        for tool in self.tools:
            self.registry.register(tool)

    def invoke(self, tool_name, req, call_id="c1", **kwargs):
        return asyncio.run(self.registry.invoke(ToolCall(call_id, tool_name, req), **kwargs))


def test_yellow_needs_policy_or_grant(work):
    name = script(work, "hi.py", "print('hi')\n")
    rig = Rig(work)
    result = rig.invoke("shell.run", request(args=[name]))
    assert (
        result.status is ToolStatus.DENIED and result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    )
    allowed = Rig(work, allow_yellow={"shell.run"})
    result = allowed.invoke("shell.run", request(args=[name]))
    assert result.status is ToolStatus.OK and result.output["stdout"] == "hi\n"
    assert allowed.audit.records[-1].permission_reason == "policy_allowed"


def test_out_of_scope_requests_are_denied_before_running(work, tmp_path):
    marker = work / "ran"
    name = script(work, "touch.py", "open('ran', 'w').write('x')\n")
    (work / "escape").symlink_to(tmp_path)
    rig = Rig(work, allow_yellow={"shell.run"})
    bad_requests = [
        request(command="sh", args=["-c", "echo hi"]),
        request(command="/bin/sh"),
        request(args=[name], root="home"),
        request(args=[name], path="escape"),
        request(args=[name], path="../"),
        request(args=["../outside.py"]),
        request(args=[name, "a", "b", "c", "d", "e"]),
        request(args=["-c", "print(1)"]),
        request(args=["--", name]),
    ]
    for index, req in enumerate(bad_requests):
        result = rig.invoke("shell.run", req, call_id=f"c{index}")
        assert (
            result.status is ToolStatus.DENIED and result.error is ToolErrorCode.PERMISSION_DENIED
        )
        assert rig.audit.records[-1].permission_reason == "out_of_scope", req
    assert not marker.exists()
    assert rig.shell_audit.records == ()  # nothing reached the tool
    # A grant cannot override scope.
    call = ToolCall("g1", "shell.run", request(command="sh"))
    grant = ConfirmationGrant.for_call(call, datetime.now(UTC) + timedelta(minutes=5))
    result = rig.invoke("shell.run", request(command="sh"), call_id="g1", grant=grant)
    assert result.error is ToolErrorCode.PERMISSION_DENIED
    # Schema violations never get that far either.
    result = rig.invoke("shell.run", {"command": "py"})
    assert result.status is ToolStatus.INVALID_ARGUMENTS


def test_invalid_input_shapes_are_rejected_by_schema(work):
    rig = Rig(work, allow_yellow={"shell.run"})
    for req in (
        {"command": "py", "cwd": {"root": "work"}, "args": [5]},
        {"command": "py", "cwd": {"root": "work"}, "args": "ls"},
        {"command": "py", "cwd": "work"},
        {"command": "py", "cwd": {"root": "work"}, "executable": "/bin/sh"},
        {"command": "py", "cwd": {"root": "work"}, "args": ["x" * 2000]},
        {"command": "py", "cwd": {"root": "work"}, "args": ["a"] * 65},
        {"command": "", "cwd": {"root": "work"}},
    ):
        assert rig.invoke("shell.run", req).status is ToolStatus.INVALID_ARGUMENTS


def test_red_requires_a_one_time_argument_bound_grant(work):
    name = script(work, "mark.py", "open('marker', 'a').write('x')\nprint('ran')\n")
    red = py_command(work, name="danger", permission=PermissionLevel.RED)
    rig = Rig(work, red)
    req = request(command="danger", args=[name])
    tool_name = "shell.run_confirmed"
    expires = datetime.now(UTC) + timedelta(minutes=5)

    denied = rig.invoke(tool_name, req, call_id="r1")
    assert denied.status is ToolStatus.DENIED
    assert denied.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert not (work / "marker").exists()

    wrong_args = ConfirmationGrant.for_call(
        ToolCall("r1", tool_name, request(command="danger", args=[name, "other"])), expires
    )
    result = rig.invoke(tool_name, req, call_id="r1", grant=wrong_args)
    assert result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert rig.audit.records[-1].permission_reason == "grant_arguments_mismatch"
    assert not (work / "marker").exists()

    grant = ConfirmationGrant.for_call(ToolCall("r1", tool_name, req), expires)
    ok = rig.invoke(tool_name, req, call_id="r1", grant=grant)
    assert ok.status is ToolStatus.OK and ok.output["stdout"] == "ran\n"
    assert rig.audit.records[-1].confirmed is True
    assert rig.shell_audit.records[-1].confirmed is True
    assert (work / "marker").read_text() == "x"

    replay = rig.invoke(tool_name, req, call_id="r1", grant=grant)
    assert replay.status is ToolStatus.DENIED
    assert rig.audit.records[-1].permission_reason == "grant_replayed"
    assert (work / "marker").read_text() == "x"  # not run a second time


def test_red_command_is_not_reachable_through_the_yellow_tool(work):
    rig = Rig(
        work,
        py_command(work),
        py_command(work, name="danger", permission=PermissionLevel.RED),
        allow_yellow={"shell.run"},
    )
    result = rig.invoke("shell.run", request(command="danger"))
    assert result.error is ToolErrorCode.PERMISSION_DENIED  # unknown to this tool: out of scope


def test_tool_is_its_own_second_gate(work):
    marker = work / "ran"
    name = script(work, "touch.py", "open('ran', 'w').write('x')\n")
    # No scope checks in the policy: the tool must still refuse everything out of scope.
    rig = Rig(work, allow_yellow={"shell.run"}, scope=False)
    result = rig.invoke("shell.run", request(args=["../escape.py"]))
    assert result.status is ToolStatus.ERROR and result.error is ToolErrorCode.INTERNAL_ERROR
    record = rig.shell_audit.records[-1]
    assert record.outcome is ShellOutcome.REJECTED and record.reason is R.ARG_VALUE_INVALID
    assert rig.invoke("shell.run", request(command="nope")).status is ToolStatus.ERROR
    assert rig.shell_audit.records[-1].reason is R.UNKNOWN_COMMAND
    assert rig.shell_audit.records[-1].command == "<unknown>"
    assert not marker.exists()
    ok = rig.invoke("shell.run", request(args=[name]), call_id="c9")
    assert ok.status is ToolStatus.OK and marker.exists()

    # Red entries: a missing confirmation is refused by the tool even without a policy.
    red = py_command(work, name="danger", permission=PermissionLevel.RED)
    red_tool = make_tool(red)
    with pytest.raises(ShellRejected) as info:
        run(red_tool, request(command="danger", args=[name]))
    assert info.value.reason is R.CONFIRMATION_MISSING
    marker.unlink()
    run(red_tool, request(command="danger", args=[name]), confirmed=True)
    assert marker.exists()

    # A context whose level or tool name does not match the entry is refused.
    tool = make_tool(py_command(work))
    for bad in (
        dataclasses.replace(context(tool), permission=PermissionLevel.RED),
        dataclasses.replace(context(tool), tool_name="shell.other"),
    ):
        with pytest.raises(ShellRejected) as info:
            asyncio.run(tool.run(request(args=[name]), bad))
        assert info.value.reason is R.PERMISSION_MISMATCH


def test_green_readonly_entries_run_without_confirmation(work):
    closed = ArgumentPolicy(flags=(FlagRule(name="--version"),))
    green = py_command(
        work, name="ver", permission=PermissionLevel.GREEN, read_only=True, argument_policy=closed
    )
    rig = Rig(work, green)
    result = rig.invoke("shell.run_readonly", request(command="ver", args=["--version"]))
    assert result.status is ToolStatus.OK and result.output["stdout"].startswith("Python")
    denied = rig.invoke("shell.run_readonly", request(command="ver", args=["-V"]), call_id="c2")
    assert denied.error is ToolErrorCode.PERMISSION_DENIED


def test_registry_reports_timeouts_and_cancellation_as_facts_and_audit(work):
    name = script(work, "tree.py", SPAWN_GRANDCHILD)
    rig = Rig(work, py_command(work, timeout_seconds=1.0), allow_yellow={"shell.run"})
    result = rig.invoke("shell.run", request(args=[name]))
    assert result.status is ToolStatus.OK and result.output["timed_out"] is True
    assert rig.shell_audit.records[-1].outcome is ShellOutcome.TIMED_OUT
    assert_dead(*read_pids(work))
    (work / "pids.txt").unlink()

    slow = Rig(work, py_command(work, timeout_seconds=30), allow_yellow={"shell.run"})

    async def scenario():
        token = CancellationToken()
        task = asyncio.ensure_future(
            slow.registry.invoke(
                ToolCall("k1", "shell.run", request(args=[name])), cancellation=token
            )
        )
        await wait_for_file(work / "pids.txt")
        token.cancel()
        return await asyncio.wait_for(task, 10)

    outcome = asyncio.run(scenario())
    # Either the tool or the registry saw the token first; both stop the group and record it.
    assert outcome.status in (ToolStatus.OK, ToolStatus.CANCELLED)
    if outcome.status is ToolStatus.OK:
        assert outcome.output["cancelled"] is True
    assert_dead(*read_pids(work))
    assert slow.shell_audit.records[-1].outcome is ShellOutcome.CANCELLED


def test_output_validates_against_the_declared_schema(work):
    name = script(work, "big.py", "import sys\nsys.stdout.write('\\u00e9' * 100000)\n")
    rig = Rig(
        work,
        py_command(work, max_output_bytes=shell.MAX_OUTPUT_BYTES),
        allow_yellow={"shell.run"},
    )
    result = rig.invoke("shell.run", request(args=[name]))
    assert result.status is ToolStatus.OK and result.output["truncated"] is True
    assert result.output["stdout_bytes"] == shell.MAX_OUTPUT_BYTES


# Audit content ---------------------------------------------------------------------------------


def test_audit_records_hold_no_arguments_or_output(work):
    marker_arg, marker_out = "ARGMARK-8c1f2e", "OUTMARK-5d7a90"
    name = script(work, "echo.py", f"import sys\nprint('{marker_out}', sys.argv[1])\n")
    rig = Rig(work, allow_yellow={"shell.run"})
    req = request(args=[name, marker_arg], path=".")
    result = rig.invoke("shell.run", req)
    assert marker_out in result.output["stdout"] and marker_arg in result.output["stdout"]
    record = rig.shell_audit.records[-1]
    text = repr(record) + repr(rig.audit.records[-1])
    assert marker_arg not in text and marker_out not in text and name not in text
    assert str(work) not in text
    assert (
        record.argument_digest
        == hashlib.sha256(canonical_json([name, marker_arg]).encode()).hexdigest()
    )
    assert record.argument_count == 2 and record.cwd_root == "work"
    assert record.stdout_bytes == len(result.output["stdout"].encode())
    assert record.command == "py" and record.permission is PermissionLevel.YELLOW
    assert record.outcome is ShellOutcome.COMPLETED and record.exit_code == 0
    assert record.cleanup_complete is True and record.reason is None

    class Failing:
        def record(self, record):
            raise RuntimeError("sink down")

    tool = make_tool(py_command(work), audit=Failing())
    assert run(tool, request(args=[name, "x"]))["exit_code"] == 0  # audit failure is not fatal


# Result verification ---------------------------------------------------------------------------


def ok_result(work, body: str, **ctx) -> ToolResult:
    name = script(work, "task.py", body)
    rig = Rig(work, allow_yellow={"shell.run"})
    return rig.invoke("shell.run", request(args=[name]))


def test_exit_zero_is_not_success_without_a_verifier(work):
    result = ok_result(work, "print('done')\n")
    assert result.output["exit_code"] == 0
    outcome = verify_shell_result(result, None)
    assert outcome.status is VerificationStatus.UNVERIFIED and outcome.reason_code == "no_verifier"


def test_verifier_judges_the_postcondition(work):
    result = ok_result(work, "print('done')\n")  # exits 0 but never writes the expected file

    def verifier(facts: ShellFacts) -> VerificationOutcome:
        assert facts.exit_zero
        if (work / "expected.txt").exists():
            return VerificationOutcome.passed()
        return VerificationOutcome.failed("file_missing")

    outcome = verify_shell_result(result, verifier)
    assert (outcome.status, outcome.reason_code) == (VerificationStatus.FAILED, "file_missing")
    (work / "expected.txt").write_text("x")
    assert verify_shell_result(result, verifier).status is VerificationStatus.VERIFIED


def test_verifier_failures_and_bad_results_never_verify(work):
    result = ok_result(work, "print('done')\n")

    def boom(facts):
        raise RuntimeError("x")

    assert verify_shell_result(result, boom).reason_code == "verifier_error"
    assert verify_shell_result(result, lambda facts: True).reason_code == "verifier_invalid"
    denied = ToolResult("c1", "shell.run", ToolStatus.DENIED, error=ToolErrorCode.PERMISSION_DENIED)
    assert verify_shell_result(denied, lambda facts: VerificationOutcome.passed()).status is (
        VerificationStatus.FAILED
    )
    broken = ToolResult("c1", "shell.run", ToolStatus.OK, output={"stdout": "x"})
    assert verify_shell_result(broken, None).reason_code == "malformed_output"
    with pytest.raises(ValueError):
        VerificationOutcome(VerificationStatus.FAILED, "Bad Code")


def test_exit_zero_helper_requires_a_clean_run():
    base = {
        "command": "py", "exit_code": 0, "signal": 0, "timed_out": False, "cancelled": False,
        "truncated": False, "stdout": "", "stderr": "", "duration_ms": 1.0,
        "cleanup_complete": True,
    }  # fmt: skip
    assert ShellFacts.from_output(base).exit_zero
    for change in ({"exit_code": 1}, {"signal": 9}, {"timed_out": True}, {"truncated": True}):
        assert not ShellFacts.from_output({**base, **change}).exit_zero


# git builder -----------------------------------------------------------------------------------


@pytest.fixture
def git_cmd(work):
    return git_readonly_command(PYTHON, {"repo": str(work)})


GIT_ACCEPT = [
    (["status"], ("status",)),
    (["status", "--short", "--branch"], ("status", "--short", "--branch")),
    (["status", "--untracked-files=no", "--", "a.py"], None),
    (
        ["log", "-n", "5", "--oneline"],
        ("log", "--no-ext-diff", "--no-textconv", "-n", "5", "--oneline"),
    ),
    (["log", "--max-count=3", "main..HEAD"], None),
    (["log", "-5", "--stat", "--", "src"], None),
    (["diff"], ("diff", "--no-ext-diff", "--no-textconv")),
    (["diff", "--cached", "--stat"], None),
    (["diff", "HEAD~1", "HEAD", "--name-only"], None),
    (["diff", "-U", "5", "--unified=3"], None),
    (["log", "--", "-rf"], None),  # after `--` a leading dash is a path (still inside the cwd)
]
GIT_REJECT = [  # every entry must raise
    (["-c", "core.pager=sh", "status"], R.ARG_SUBCOMMAND),
    (["--git-dir=/x", "status"], R.ARG_SUBCOMMAND),
    (["--exec-path=/x", "status"], R.ARG_SUBCOMMAND),
    (["status", "-c", "core.fsmonitor=/x"], R.ARG_FORBIDDEN),
    (["status", "-ccore.pager=sh"], R.ARG_FORBIDDEN),
    (["log", "--exec-path=/x"], R.ARG_FORBIDDEN),
    (["log", "--exec-path"], R.ARG_FORBIDDEN),
    (["log", "--upload-pack=sh"], R.ARG_FORBIDDEN),
    (["log", "--receive-pack", "sh"], R.ARG_FORBIDDEN),
    (["diff", "--ext-diff"], R.ARG_FORBIDDEN),
    (["diff", "--textconv"], R.ARG_FORBIDDEN),
    (["diff", "--output=out.txt"], R.ARG_FORBIDDEN),
    (["diff", "--output", "out.txt"], R.ARG_FORBIDDEN),
    (["log", "-O", "orderfile"], R.ARG_FORBIDDEN),
    (["log", "--pager=sh"], R.ARG_FORBIDDEN),
    (["log", "--paginate"], R.ARG_FORBIDDEN),
    (["diff", "--open-files-in-pager=sh"], R.ARG_FORBIDDEN),
    (["diff", "--no-index", "a", "b"], R.ARG_FORBIDDEN),
    (["status", "--git-dir=/x"], R.ARG_FORBIDDEN),
    (["status", "--work-tree=/x"], R.ARG_FORBIDDEN),
    (["status", "-C", "/"], R.ARG_FORBIDDEN),
    (["log", "--config", "x=y"], R.ARG_FORBIDDEN),
    (["config", "alias.x", "!sh"], R.ARG_SUBCOMMAND),
    (["alias"], R.ARG_SUBCOMMAND),
    (["!sh"], R.ARG_SUBCOMMAND),
    (["push"], R.ARG_SUBCOMMAND),
    (["fetch"], R.ARG_SUBCOMMAND),
    (["clone", "x"], R.ARG_SUBCOMMAND),
    (["checkout", "."], R.ARG_SUBCOMMAND),
    (["reset", "--hard"], R.ARG_SUBCOMMAND),
    (["submodule", "foreach", "sh"], R.ARG_SUBCOMMAND),
    (["status;ls"], R.ARG_SUBCOMMAND),
    (["log", "--format=%H"], R.ARG_NOT_ALLOWED),
    (["log", "--since=1.day"], R.ARG_NOT_ALLOWED),
    (["log", "--all"], R.ARG_NOT_ALLOWED),
    (["log", "-5000"], R.ARG_NOT_ALLOWED),
    (["status", "-uall"], R.ARG_NOT_ALLOWED),
    (["log", "-n", "0"], R.ARG_VALUE_INVALID),
    (["log", "-n", "201"], R.ARG_VALUE_INVALID),
    (["log", "--max-count=abc"], R.ARG_VALUE_INVALID),
    (["status", "--untracked-files=bogus"], R.ARG_VALUE_INVALID),
    (["log", "-"], R.ARG_NOT_ALLOWED),
    (["log", "a;b"], R.ARG_VALUE_INVALID),
    (["log", "$(id)"], R.ARG_VALUE_INVALID),
    (["log", "--", "/etc/passwd"], R.ARG_VALUE_INVALID),
    (["log", "--", "../x"], R.ARG_VALUE_INVALID),
    (["diff", "a", "b", "c"], R.ARG_TOO_MANY_POSITIONALS),
    (["log", "a", "b", "c", "d", "e"], R.ARG_TOO_MANY_POSITIONALS),
    (["status", "HEAD"], R.ARG_TOO_MANY_POSITIONALS),
    (["status", "--short", "--short"], R.ARG_REPEATED),
    ([], R.ARG_SUBCOMMAND),
]


@pytest.mark.parametrize(("args", "expected"), GIT_ACCEPT)
def test_git_policy_accepts(git_cmd, work, args, expected):
    argv = git_cmd.argument_policy.parse(args, os.path.realpath(work))
    if expected is not None:
        assert argv == expected


@pytest.mark.parametrize(("args", "reason"), GIT_REJECT)
def test_git_policy_rejects(git_cmd, work, args, reason):
    cwd = os.path.realpath(work)
    with pytest.raises(ShellRejected) as info:
        git_cmd.argument_policy.parse(args, cwd)
    assert info.value.reason is reason


def test_git_command_shape(git_cmd, monkeypatch, work):
    assert git_cmd.permission is PermissionLevel.YELLOW and git_cmd.read_only
    assert git_cmd.env_allowlist == ()
    fixed = git_cmd.fixed_args
    assert fixed[:2] == ("--no-pager", "--no-optional-locks")
    assert ("-c", "core.fsmonitor=false") in zip(fixed, fixed[1:], strict=False)
    assert git_cmd.env_fixed["GIT_CONFIG_NOSYSTEM"] == "1"
    for name in (
        "GIT_EXTERNAL_DIFF",
        "GIT_SSH_COMMAND",
        "GIT_PAGER",
        "GIT_DIR",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.setenv(name, "/evil/value")
    env = build_environment(git_cmd, os.environ, "/scratch")
    assert env["GIT_PAGER"] == "cat" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    for name in ("GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND", "GIT_DIR", "GIT_CONFIG_COUNT"):
        assert name not in env
    assert "/evil/value" not in env.values()


def test_git_command_is_not_green(work):
    assert git_readonly_command(PYTHON, {"repo": str(work)}).argument_policy.is_closed() is False
    with pytest.raises(ShellConfigError):
        dataclasses.replace(
            git_readonly_command(PYTHON, {"repo": str(work)}), permission=PermissionLevel.GREEN
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_status_does_not_run_a_configured_fsmonitor_hook(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    clean_env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path), "LC_ALL": "C"}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, env=clean_env, capture_output=True, text=True, timeout=30
        )

    if git("init", "-q").returncode != 0:
        pytest.skip("git init failed")
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nprintf '\\0'\n")
    hook.chmod(0o755)
    git("config", "core.fsmonitor", str(hook))
    git("status", "--short")
    if not marker.exists():
        pytest.skip("this git version does not run the fsmonitor hook for status")
    marker.unlink()

    command = git_readonly_command(shutil.which("git"), {"repo": str(repo)}, timeout_seconds=30)
    tool = make_tool(command)
    (repo / "new.txt").write_text("x")
    result = run(tool, request(command="git_ro", args=["status", "--short"], root="repo"))
    assert result["exit_code"] == 0 and "new.txt" in result["stdout"], result["stderr"]
    assert not marker.exists()


# pytest builder --------------------------------------------------------------------------------


@pytest.fixture
def pytest_cmd(work):
    return pytest_command(PYTHON, {"proj": str(work)})


PYTEST_ACCEPT = [
    ["-q"],
    ["-q", "-x", "--maxfail=3", "--tb=short", "--no-header", "--disable-warnings"],
    ["-k", "alpha and not beta", "-m", "slow or fast"],
    ["tests/test_x.py::test_y[1]", "tests"],
    ["--durations=5", "tests/test_x.py"],
]
PYTEST_REJECT = [
    (["-p", "evil_plugin"], R.ARG_FORBIDDEN),
    (["-pevil_plugin"], R.ARG_FORBIDDEN),
    (["-c", "evil.ini"], R.ARG_FORBIDDEN),
    (["-o", "addopts=-p evil"], R.ARG_FORBIDDEN),
    (["-oaddopts=x"], R.ARG_FORBIDDEN),
    (["--override-ini=addopts=x"], R.ARG_FORBIDDEN),
    (["--rootdir=/"], R.ARG_FORBIDDEN),
    (["--confcutdir=/"], R.ARG_FORBIDDEN),
    (["--basetemp=/tmp/x"], R.ARG_FORBIDDEN),
    (["--junitxml=out.xml"], R.ARG_FORBIDDEN),
    (["--junit-xml", "out.xml"], R.ARG_FORBIDDEN),
    (["--pdb"], R.ARG_FORBIDDEN),
    (["--pdbcls=evil:Debugger"], R.ARG_FORBIDDEN),
    (["--trace"], R.ARG_FORBIDDEN),
    (["--import-mode=importlib"], R.ARG_FORBIDDEN),
    (["--pyargs", "os"], R.ARG_FORBIDDEN),
    (["--log-file=out.log"], R.ARG_FORBIDDEN),
    (["--doctest-modules"], R.ARG_FORBIDDEN),
    (["--cache-clear"], R.ARG_FORBIDDEN),
    (["-n", "4"], R.ARG_NOT_ALLOWED),
    (["--collect-only"], R.ARG_NOT_ALLOWED),
    (["-s"], R.ARG_NOT_ALLOWED),
    (["--maxfail=0"], R.ARG_VALUE_INVALID),
    (["--maxfail", "3"], R.ARG_NOT_ALLOWED),
    (["--tb=evil"], R.ARG_VALUE_INVALID),
    (["-k", "-x"], R.ARG_VALUE_INVALID),
    (["-k", "a;b"], R.ARG_VALUE_INVALID),
    (["-k", "$(id)"], R.ARG_VALUE_INVALID),
    (["-k", "x" * 300], R.ARG_LENGTH),
    (["-k"], R.ARG_MISSING_VALUE),
    (["/etc/passwd"], R.ARG_VALUE_INVALID),
    (["../outside"], R.ARG_VALUE_INVALID),
    (["tests/../../outside"], R.ARG_VALUE_INVALID),
    (["--"], R.ARG_DOUBLE_DASH),
    (["a"] * 17, R.ARG_TOO_MANY_POSITIONALS),
    (["-q", "-q"], R.ARG_REPEATED),
]


@pytest.mark.parametrize("args", PYTEST_ACCEPT)
def test_pytest_policy_accepts(pytest_cmd, work, args):
    assert pytest_cmd.argument_policy.parse(args, os.path.realpath(work)) == tuple(args)


@pytest.mark.parametrize(("args", "reason"), PYTEST_REJECT)
def test_pytest_policy_rejects(pytest_cmd, work, args, reason):
    with pytest.raises(ShellRejected) as info:
        pytest_cmd.argument_policy.parse(args, os.path.realpath(work))
    assert info.value.reason is reason


def test_pytest_command_shape(monkeypatch, work):
    command = pytest_command(PYTHON, {"proj": str(work)})
    assert command.permission is PermissionLevel.YELLOW and not command.read_only
    assert command.fixed_args[:2] == ("-m", "pytest")
    assert command.env_fixed["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    with_plugins = pytest_command(PYTHON, {"proj": str(work)}, autoload_plugins=True)
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in with_plugins.env_fixed
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP"):
        monkeypatch.setenv(name, "evil_plugin")
    env = build_environment(command, os.environ, "/scratch")
    assert not {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP"} & set(env)
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in env


def test_pytest_builder_runs_a_tiny_project(work):
    """End to end through the tool; a stub `pytest` package stands in when the resolved
    interpreter lacks pytest (venv interpreters resolve to their base), so it never skips."""
    probe = subprocess.run(
        [os.path.realpath(PYTHON), "-c", "import pytest"], capture_output=True, timeout=30
    )
    if probe.returncode != 0:
        stub = work / "pytest"
        stub.mkdir()
        (stub / "__init__.py").write_text("")
        (stub / "__main__.py").write_text("print('1 passed')\n")
    (work / "test_tiny.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    tool = make_tool(pytest_command(PYTHON, {"proj": str(work)}, timeout_seconds=60))
    result = run(tool, request(command="pytest", args=["-q", "test_tiny.py"], root="proj"))
    assert result["exit_code"] == 0 and "1 passed" in result["stdout"], result["stderr"]
    assert not (work / ".pytest_cache").exists()


# Misc ------------------------------------------------------------------------------------------


def test_modes_of_scratch_dirs_are_private(work, monkeypatch):
    name = script(
        work,
        "mode.py",
        """
        import os, stat
        print(oct(stat.S_IMODE(os.stat(os.environ["HOME"]).st_mode)))
        """,
    )
    result = run(make_tool(py_command(work)), request(args=[name]))
    assert result["stdout"].strip() == oct(stat.S_IRWXU)
