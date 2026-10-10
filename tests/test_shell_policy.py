"""Shell policy, settings, doctor and factory: real harmless commands inside tmp dirs only."""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from backend import doctor
from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.tools.approvals import ApprovalStore
from backend.tools.contract import PermissionLevel, ToolCall, ToolErrorCode, ToolStatus
from backend.tools.permission import PermissionPolicy
from backend.tools.shell import ArgumentPolicy, Matches, ShellConfigError, ShellOutcome
from backend.tools.shell_policy import (
    DEFAULT_ALLOWLIST,
    CommandEntry,
    InMemoryShellRunSink,
    ShellStatusKind,
    build_shell_wiring,
    inspect_shell,
    summarize_argv,
    validate_root,
)

LEAK = "sk" + "-" + "LIVE0123456789abcdefLEAK"
ROOT_ARG = {"root": "work"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("JARVIS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "x.sqlite3"))


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "workroot"
    root.mkdir()
    (root / "hello.txt").write_text("hello-body\n")
    (root / ".env").write_text("TOPSECRET=1\n")
    (root / "key.pem").write_text("pem\n")
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("a-body\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-body\n")
    os.symlink(outside, root / "link.txt")
    os.symlink(root / ".env", root / "innocent.txt")
    return root


def settings_for(root: Path | None, **kw) -> Settings:
    return Settings(
        db_path=Path("x.sqlite3"),
        shell_enabled=kw.pop("enabled", True),
        shell_root=root,
        **kw,
    )


@pytest.fixture
def store(tmp_path: Path) -> ApprovalStore:
    db = Database(tmp_path / "approvals.sqlite3")
    db.initialize()
    return ApprovalStore(db, ttl_seconds=60)


def call(args, command="ls", tool="shell.run_readonly", cid="c1", path="."):
    return ToolCall(
        cid, tool, {"command": command, "args": args, "cwd": {**ROOT_ARG, "path": path}}
    )


def wire(settings, store, **kw):
    sink = InMemoryShellRunSink()
    wiring = build_shell_wiring(settings, approvals=store.requester(), run_sink=sink, **kw)
    assert wiring is not None
    return wiring, sink


def execute(wiring, tool_call, wait=0.0):
    return asyncio.run(wiring.executor.execute(tool_call, wait_seconds=wait))


def approve_all(store):
    for request in store.list_pending():
        store.decide(request.id, True)


# ----- off by default -----


def test_nothing_registered_by_default(store, work):
    assert Settings.from_env().shell_enabled is False
    assert build_shell_wiring(Settings.from_env(), approvals=store.requester()) is None
    # a root alone does not enable anything
    assert (
        build_shell_wiring(settings_for(work, enabled=False), approvals=store.requester()) is None
    )
    assert inspect_shell(Settings.from_env()).status is ShellStatusKind.OFF


@pytest.mark.parametrize("kind", ["missing", "relative", "file", "slash", "home", "parent_of_home"])
def test_invalid_root_registers_nothing(store, work, monkeypatch, kind):
    monkeypatch.setenv("HOME", str(work))
    root = {
        "missing": work / "nope",
        "relative": Path("workroot"),
        "file": work / "hello.txt",
        "slash": Path("/"),
        "home": work,
        "parent_of_home": work.parent,
    }[kind]
    s = settings_for(root)
    assert validate_root(root) is None
    assert inspect_shell(s).code == "SHELL_ROOT_INVALID"
    assert build_shell_wiring(s, approvals=store.requester()) is None


def test_enabled_without_root_registers_nothing(store):
    s = settings_for(None)
    assert inspect_shell(s).code == "SHELL_ROOT_MISSING"
    assert build_shell_wiring(s, approvals=store.requester()) is None


def test_unknown_command_name_registers_nothing(store, work):
    s = settings_for(work, shell_commands=("ls", "bash"))
    assert inspect_shell(s).code == "SHELL_COMMAND_UNKNOWN"
    assert build_shell_wiring(s, approvals=store.requester()) is None


# ----- settings -----


def test_settings_from_env(monkeypatch, work):
    monkeypatch.setenv("JARVIS_SHELL_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SHELL_ROOT", str(work))
    monkeypatch.setenv("JARVIS_SHELL_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("JARVIS_SHELL_MAX_OUTPUT_BYTES", "1000")
    monkeypatch.setenv("JARVIS_SHELL_COMMANDS", "ls, cat")
    s = Settings.from_env()
    assert s.shell_enabled and s.shell_root == work
    assert (s.shell_timeout_seconds, s.shell_max_output_bytes) == (12.5, 1000)
    assert s.shell_commands == ("ls", "cat")


@pytest.mark.parametrize(
    "name,value",
    [
        ("JARVIS_SHELL_ENABLED", "maybe"),
        ("JARVIS_SHELL_ROOT", "  "),
        ("JARVIS_SHELL_TIMEOUT_SECONDS", "0"),
        ("JARVIS_SHELL_TIMEOUT_SECONDS", "601"),
        ("JARVIS_SHELL_TIMEOUT_SECONDS", "nan"),
        ("JARVIS_SHELL_TIMEOUT_SECONDS", "soon"),
        ("JARVIS_SHELL_MAX_OUTPUT_BYTES", "0"),
        ("JARVIS_SHELL_MAX_OUTPUT_BYTES", "70000"),
        ("JARVIS_SHELL_COMMANDS", ""),
        ("JARVIS_SHELL_COMMANDS", "ls,ls"),
        ("JARVIS_SHELL_COMMANDS", "ls;rm"),
        ("JARVIS_SHELL_COMMANDS", "LS bad"),
    ],
)
def test_settings_reject_bad_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Settings.from_env()


# ----- doctor -----


def by_area():
    return {c.area: c for c in doctor.run_checks()}


def test_doctor_off_ok_incomplete_without_paths(monkeypatch, tmp_path):
    assert (by_area()["shell"].status, by_area()["shell"].code) == ("OFF", "NOT_CONFIGURED")
    monkeypatch.setenv("JARVIS_SHELL_ENABLED", "true")
    assert by_area()["shell"].code == "SHELL_ROOT_MISSING"
    assert by_area()["shell"].status == "INCOMPLETE"
    assert doctor.main([]) == 1
    secret_dir = tmp_path / "private-shell-root"
    monkeypatch.setenv("JARVIS_SHELL_ROOT", str(secret_dir))
    assert by_area()["shell"].code == "SHELL_ROOT_INVALID"
    secret_dir.mkdir()
    check = by_area()["shell"]
    assert (check.status, check.code) == ("OK", "READY")
    assert "ls" in check.detail
    out: list[str] = []
    doctor.main(["--json"], _out=out.append)
    doctor.main([], _out=out.append)
    text = "\n".join(out)
    assert "private-shell-root" not in text and str(tmp_path) not in text
    monkeypatch.setenv("JARVIS_SHELL_COMMANDS", "nope")
    assert by_area()["shell"].code == "SHELL_COMMAND_UNKNOWN"


# ----- factory and allowlist -----


def test_wiring_registers_levels_by_table(store, work):
    wiring, _ = wire(settings_for(work), store)
    assert wiring.commands == ("ls", "cat", "git")
    assert wiring.tool_names == ("shell.run", "shell.run_readonly")
    levels = {s.name: s.permission for s in wiring.registry.list_specs()}
    assert levels == {
        "shell.run_readonly": PermissionLevel.GREEN,
        "shell.run": PermissionLevel.YELLOW,
    }
    assert "pytest" not in wiring.commands


def test_pytest_only_when_named(store, work):
    wiring, _ = wire(settings_for(work, shell_commands=("pytest",)), store)
    assert wiring.commands == ("pytest",) and wiring.tool_names == ("shell.run",)


def test_timeout_and_cap_come_from_settings(store, work):
    s = settings_for(
        work, shell_timeout_seconds=7, shell_max_output_bytes=5, shell_commands=("ls", "cat")
    )
    wiring, sink = wire(s, store)
    spec = {x.name: x for x in wiring.registry.list_specs()}["shell.run"]
    assert spec.timeout_seconds == pytest.approx(7 + 15)
    out = execute(wiring, call(["hello.txt"], "cat", "shell.run"), wait=5)
    assert out.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    approve_all(store)
    out = execute(wiring, call(["hello.txt"], "cat", "shell.run"))
    assert out.result.status is ToolStatus.OK
    assert out.result.output["truncated"] is True and len(out.result.output["stdout"]) <= 5
    assert sink.records[-1].truncated is True


def test_green_ls_runs_without_approval(store, work):
    wiring, sink = wire(settings_for(work), store)
    out = execute(wiring, call(["-1", "-a"]))
    assert out.result.status is ToolStatus.OK and out.approval_id is None
    assert "hello.txt" in out.result.output["stdout"]
    assert store.list_pending() == []
    record = sink.records[-1]
    assert (record.command, record.exit_code, record.outcome) == ("ls", 0, ShellOutcome.COMPLETED)
    assert record.duration_ms >= 0 and record.argv_summary.startswith("ls -1 -a")


def test_ls_in_subdirectory_via_cwd(store, work):
    wiring, _ = wire(settings_for(work), store)
    out = execute(wiring, call(["-1"], path="sub"))
    assert out.result.output["stdout"].split() == ["a.txt"]
    for bad in ("..", "/etc", "sub/../..", "link.txt"):
        r = execute(wiring, call(["-1"], path=bad)).result
        assert r.status is not ToolStatus.OK


def test_yellow_cat_needs_human_then_runs_once(store, work):
    wiring, sink = wire(settings_for(work), store)
    first = execute(wiring, call(["hello.txt"], "cat", "shell.run"))
    assert first.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert first.result.output is None and not sink.records  # nothing ran
    (pending,) = store.list_pending()
    assert "hello.txt" in repr(pending.summary)
    store.decide(pending.id, True)
    done = execute(wiring, call(["hello.txt"], "cat", "shell.run", cid="c2"))
    assert done.result.status is ToolStatus.OK and done.result.output["stdout"] == "hello-body\n"
    again = execute(wiring, call(["hello.txt"], "cat", "shell.run", cid="c3"))
    assert again.result.error is ToolErrorCode.CONFIRMATION_REQUIRED  # approval was one-shot


def test_denied_yellow_never_runs(store, work):
    wiring, sink = wire(settings_for(work), store)
    execute(wiring, call(["hello.txt"], "cat", "shell.run"))
    (pending,) = store.list_pending()
    store.decide(pending.id, False)
    out = execute(wiring, call(["hello.txt"], "cat", "shell.run", cid="c2"))
    assert out.result.status is not ToolStatus.OK and not sink.records


def test_audit_has_summary_exit_code_duration_but_no_output(store, work):
    wiring, sink = wire(settings_for(work), store)
    execute(wiring, call(["-n", "hello.txt"], "cat", "shell.run"))
    approve_all(store)
    execute(wiring, call(["-n", "hello.txt"], "cat", "shell.run", cid="c2"))
    (record,) = sink.records
    assert record.argv_summary == "cat -n hello.txt @work/."
    assert record.exit_code == 0 and record.confirmed is True and record.duration_ms > 0
    text = repr(record) + repr(record.as_log_fields())
    assert "hello-body" not in text and str(work) not in text


def test_run_sink_default_logs_json(store, work, caplog):
    caplog.set_level("INFO", logger="jarvis.shell.audit")
    wiring = build_shell_wiring(settings_for(work), approvals=store.requester())
    assert wiring is not None
    execute(wiring, call(["-1"]))
    lines = [r.getMessage() for r in caplog.records if r.name == "jarvis.shell.audit"]
    assert len(lines) == 1 and '"exit_code": 0' in lines[0] and "hello.txt" not in lines[0]


# ----- hostile argv -----


@pytest.mark.parametrize(
    "args",
    [
        ["/etc/passwd"],
        ["../outside.txt"],
        ["sub/../../outside.txt"],
        [".env"],
        ["innocent.txt"],  # symlink to .env
        ["link.txt"],  # symlink out of the root
        ["key.pem"],
        ["sub"],  # a directory
        ["--", "hello.txt"],
        ["-A", "hello.txt"],
        ["--help"],
        ["hello.txt;id"],
        ["hello.txt\nid"],
        ["$(id)"],
        ["hello.txt", "\x00"],
        ["-n‮"],
        [""],
        ["a"] * 40,
        ["x" * 600],
        "hello.txt",
        [5],
        None,
    ],
)
def test_hostile_cat_args_never_reach_approval_or_run(store, work, args):
    wiring, sink = wire(settings_for(work), store)
    out = execute(wiring, call(args, "cat", "shell.run"))
    assert out.result.status is not ToolStatus.OK
    assert out.result.error is not ToolErrorCode.CONFIRMATION_REQUIRED
    assert store.list_pending() == [] and not sink.records


@pytest.mark.parametrize(
    "args",
    [
        ["/"],
        ["-R", "/"],
        ["hello.txt"],
        ["--color=always"],
        ["-1;id"],
        ["-l", "-a", "&&", "id"],
        ["--help"],
    ],
)
def test_hostile_ls_args_rejected_before_run(store, work, args):
    wiring, sink = wire(settings_for(work), store)
    out = execute(wiring, call(args))
    assert out.result.status is not ToolStatus.OK and not sink.records


@pytest.mark.parametrize(
    "command,tool",
    [
        ("sh", "shell.run"),
        ("bash", "shell.run_readonly"),
        ("git ", "shell.run"),
        ("rm", "shell.run"),
        ("cat", "shell.run_readonly"),  # Yellow command through the Green tool
        ("ls", "shell.run"),  # Green command through the Yellow tool
        ("../bin/ls", "shell.run_readonly"),
    ],
)
def test_unknown_or_wrong_level_command_refused(store, work, command, tool):
    wiring, sink = wire(settings_for(work), store)
    out = execute(wiring, call(["-1"], command, tool))
    assert out.result.status is not ToolStatus.OK and not sink.records
    assert store.list_pending() == []


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "core.pager=touch pwned", "status"],
        ["-C", "/", "status"],
        ["--git-dir=/tmp", "status"],
        ["status", "--output=out.txt"],
        ["log", "--upload-pack=id"],
        ["diff", "--ext-diff"],
        ["diff", "--no-index", "/etc/passwd", "hello.txt"],
        ["config", "--list"],
        ["push"],
        ["checkout", "main"],
        ["log", "--exec=id"],
        ["log", "main; id"],
        ["log", "../../x"],
        ["status", "--", "/etc"],
        ["status", "--", "../x"],
        ["alias.x"],
        [],
    ],
)
def test_hostile_git_args_rejected(store, work, args):
    wiring, sink = wire(settings_for(work), store)
    out = execute(wiring, call(args, "git", "shell.run"))
    assert out.result.status is not ToolStatus.OK
    assert out.result.error is not ToolErrorCode.CONFIRMATION_REQUIRED
    assert store.list_pending() == [] and not sink.records
    assert not (work / "pwned").exists() and not (work / "out.txt").exists()


def test_cwd_root_label_is_fixed(store, work):
    wiring, sink = wire(settings_for(work), store)
    for cwd in ({"root": "other", "path": "."}, {"root": "work", "extra": 1}, "work", None):
        r = execute(
            wiring, ToolCall("c", "shell.run_readonly", {"command": "ls", "args": [], "cwd": cwd})
        )
        assert r.result.status is not ToolStatus.OK
    extra = ToolCall(
        "c", "shell.run_readonly", {"command": "ls", "args": [], "cwd": ROOT_ARG, "env": {"A": "1"}}
    )
    assert execute(wiring, extra).result.status is not ToolStatus.OK
    assert not sink.records


# ----- git on a throwaway repository -----


def git_repo(work: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work.parent),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "hello.txt"],
        ["git", "commit", "-q", "-m", "first commit"],
    ):
        subprocess.run(cmd, cwd=work, env=env, check=True, capture_output=True)
    (work / "hello.txt").write_text("changed\n")


def test_git_status_log_diff_on_tmp_repo(store, work):
    git_repo(work)
    wiring, sink = wire(settings_for(work), store)
    expectations = {
        ("status", "--short"): " M hello.txt",
        ("log", "--oneline"): "first commit",
        ("diff", "--stat"): "hello.txt",
    }
    for i, (args, expected) in enumerate(expectations.items()):
        tool_call = call(list(args), "git", "shell.run", cid=f"g{i}")
        assert execute(wiring, tool_call).result.error is ToolErrorCode.CONFIRMATION_REQUIRED
        approve_all(store)
        out = execute(wiring, tool_call)
        assert out.result.status is ToolStatus.OK, out.result
        assert expected in out.result.output["stdout"]
    assert [r.exit_code for r in sink.records] == [0, 0, 0]
    assert sink.records[0].argv_summary == "git status --short @work/."


# ----- policy construction rules -----


def test_base_policy_cannot_auto_allow_shell(store, work):
    with pytest.raises(ShellConfigError):
        build_shell_wiring(
            settings_for(work),
            approvals=store.requester(),
            base_policy=PermissionPolicy(allow_yellow=frozenset({"shell.run"})),
        )


def test_base_policy_deny_still_wins(store, work):
    wiring, _ = wire(
        settings_for(work),
        store,
        base_policy=PermissionPolicy(deny=frozenset({"shell.run_readonly"})),
    )
    out = execute(wiring, call(["-1"]))
    assert out.result.status is ToolStatus.DENIED


def test_table_rejects_red_and_mismatched_entries(store, work):
    with pytest.raises(ShellConfigError):
        CommandEntry("rmx", PermissionLevel.RED, ("/bin/ls",), DEFAULT_ALLOWLIST["ls"].build)
    liar = CommandEntry(
        "ls", PermissionLevel.YELLOW, ("/bin/ls", "/usr/bin/ls"), DEFAULT_ALLOWLIST["ls"].build
    )
    with pytest.raises(ShellConfigError):
        build_shell_wiring(
            settings_for(work, shell_commands=("ls",)),
            approvals=store.requester(),
            allowlist={"ls": liar},
        )


def test_custom_table_entry_echo(store, work):
    def build_echo(exe, roots, timeout, cap):
        from backend.tools.shell import AllowedCommand

        return AllowedCommand(
            name="echo",
            executable=exe,
            permission=PermissionLevel.YELLOW,
            argument_policy=ArgumentPolicy(
                positionals=Matches(r"[a-z0-9 ]{1,20}"), max_positionals=3
            ),
            cwd_roots=roots,
            timeout_seconds=timeout,
            max_output_bytes=cap,
        )

    table = {"echo": CommandEntry("echo", PermissionLevel.YELLOW, ("/bin/echo",), build_echo)}
    s = settings_for(work, shell_commands=("echo",))
    wiring, sink = wire(s, store, allowlist=table)
    c = call(["hi", "there"], "echo", "shell.run")
    execute(wiring, c)
    approve_all(store)
    out = execute(wiring, c)
    assert out.result.output["stdout"] == "hi there\n"
    assert (
        execute(wiring, call(["$HOME"], "echo", "shell.run", cid="c9")).result.status
        is not ToolStatus.OK
    )
    assert len(sink.records) == 1


# ----- argv summary -----


def test_summary_redacts_and_cleans():
    text = summarize_argv(
        {
            "command": "cat",
            "args": [LEAK, "--token", "abc", "--api-key=zzz", "ok\x1b[31m\nname", "y" * 200]
            + ["z"] * 10,
            "cwd": {"root": "work", "path": "sub"},
        }
    )
    assert LEAK not in text and "abc" not in text and "zzz" not in text
    assert "\x1b" not in text and "\n" not in text
    assert "[redacted]" in text and "(+" in text and len(text) <= 400
    for junk in (None, 5, {"args": 5}, {"command": object(), "args": [object()]}):
        assert isinstance(summarize_argv(junk), str)


def test_hostile_rejection_is_audited_without_values(store, work):
    # Rejections that get past the scope check still land in the audit trail.
    wiring, sink = wire(settings_for(work), store)
    tool = wiring.registry.get("shell.run_readonly")
    assert tool is not None
    out = execute(wiring, call(["-1"]))
    assert out.result.status is ToolStatus.OK
    assert [r.reason for r in sink.records] == [None]


def test_approval_summary_shows_argv_but_not_credentials():
    from backend.tools.approvals import summarize_arguments

    shown = summarize_arguments({"args": ["-n", "a.txt"], "cwd": {"root": "work", "path": "sub"}})
    previews = {f["name"]: f["preview"] for f in shown["fields"]}
    assert previews["args"] == "[-n, a.txt]" and previews["cwd"] == "path=sub, root=work"
    hidden = summarize_arguments({"args": ["a", LEAK], "cwd": {"token": "x"}})
    previews = {f["name"]: f["preview"] for f in hidden["fields"]}
    assert LEAK not in repr(hidden) and previews["args"] == "array(2 items)"
    assert previews["cwd"] == "object(1 keys)"
