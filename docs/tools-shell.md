# Shell tool

First slice of Phase 4 / T3 (JAR-72), in `backend/tools/shell.py`. It builds on the tool contract, registry, and permission layer described in [tools.md](tools.md).

**There is no free-form shell.** The application registers named `AllowedCommand` entries. The model can only name one, pass arguments that satisfy that entry's argument grammar, and pick a working directory inside one of the entry's named roots. It never supplies an executable path, a shell string, or an environment. Nothing is registered by default: with an empty catalog there is no shell tool at all.

POSIX only. Constructing an `AllowedCommand`, `CommandCatalog`, `ShellTool`, or `build_shell_tools(...)` on another platform raises `ShellUnsupportedPlatformError` (fail closed). Process groups, `killpg`, and the child's session are POSIX concepts this slice does not emulate.

## Wiring

```python
catalog = CommandCatalog([AllowedCommand(name="lint", executable="/usr/bin/…", permission=PermissionLevel.YELLOW, ...)])
tools = build_shell_tools(catalog, audit=shell_audit_sink)           # one ShellTool per permission level in use
policy = PermissionPolicy(allow_yellow={"shell.run"}, scope_checks=shell_scope_checks(tools))
registry = ToolRegistry(policy, audit=...)
for tool in tools:
    registry.register(tool)
```

A `ToolSpec` carries one permission level, so commands are split across tools by level:

| Entry permission | Tool | Default decision from `PermissionPolicy` |
| --- | --- | --- |
| Green (`read_only=True`, closed argument policy) | `shell.run_readonly` | Allowed |
| Yellow | `shell.run` | Confirmation required unless `shell.run` is in `allow_yellow` |
| Red | `shell.run_confirmed` | Always needs a one-time `ConfirmationGrant` bound to the exact arguments digest |

A Yellow command is therefore only reachable through `shell.run`, a Red command only through `shell.run_confirmed`. Allow-listing Yellow in the policy applies to every Yellow command; if you want per-command decisions, keep that command Red or put it in its own catalog and policy.

Tool input:

```json
{"command": "<registered name>", "args": ["…"], "cwd": {"root": "<root name>", "path": "relative/dir"}}
```

`cwd.path` defaults to `.`. Extra keys are rejected.

## AllowedCommand

| Field | Meaning |
| --- | --- |
| `name` | Short lowercase label the model uses. |
| `executable` | Absolute path to an existing executable regular file. Resolved with `realpath` at registration and executed by the resolved path. |
| `permission` | Yellow or Red. Green only with `read_only=True` **and** a closed argument policy (every argument matched by an exact string, an anchored regex, or a bounded integer; no free-form positionals, paths, or text). A Red entry cannot be `read_only`. |
| `argument_policy` | Declarative grammar (below). |
| `cwd_roots` | Named absolute directories (resolved with `realpath`, not `/`). The cwd must be inside one after `realpath`. |
| `env_allowlist` | Parent variables inherited by name. Everything else is dropped. Names that look sensitive (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSW*`, `*AUTH*`, `*SESSION*`, `*COOKIE*`, `*CERT*` …), cloud/CI/SSH prefixes, `LD_*`, `DYLD_*`, `PYTHON*`, `GIT_*`, and shell-control names are refused at registration. |
| `env_fixed` | Constants chosen by the application (cannot overlap `env_allowlist`, cannot have sensitive names). |
| `timeout_seconds` | Wall clock, in (0, 600]. |
| `max_output_bytes` | Combined stdout+stderr cap, 1 to 65,536. |
| `cancellable` | Whether a cancellation token stops this command. |
| `fixed_args` | Trusted tokens placed before the validated ones (for example `-m pytest`). Not model-controlled. |

The child's environment is built from scratch: `PATH=/usr/bin:/bin`, `HOME`, `TMPDIR`, and `XDG_*` point into a fresh `0700` temporary directory that is removed after the run, plus the allow-listed and fixed variables. The parent's API keys, tokens, and credentials are never inherited, whatever their names, unless you list them in `env_allowlist` (and sensitive-looking names cannot be listed).

## Argument grammar

A command allow-list is only as safe as its arguments: many programs run other programs through a flag (`git -c core.pager=…`, `--upload-pack`, `pytest -p`, `find -exec`). The grammar allows nothing by default.

- Every token must be a non-empty string within the policy's length limit, with no control, format, surrogate, private-use, unassigned, or line/paragraph-separator characters (NUL, newlines, ESC, bidi overrides, zero-width characters). The count is bounded (default 32, at most 64).
- `subcommands`: the first argument must be exactly one of the declared names; the rest is checked by the nested policy. Parent `forbidden` patterns apply to nested subcommands. `inject_args` are trusted tokens inserted after the subcommand.
- Every token starting with `-` must match a `FlagRule` (an exact string or an anchored regex, optionally taking a value validated by a `OneOf`, `Matches`, `IntRange`, `PlainText`, or `RelPath` validator, in `--flag value`, `--flag=value`, or either style, with a maximum use count). Short-flag clusters are not expanded; list the exact forms.
- `forbidden`: anchored regexes for flags known to execute other programs. They are checked first and reported with a distinct reason (`arg_forbidden`). A flag rule that matches a forbidden pattern is a configuration error.
- Positionals are validated one by one and never start with `-` (a leading dash is only accepted after `--`, and only if the policy opted in to `--` by declaring `after_double_dash`).
- `RelPath` requires a relative path with no `..` segment that stays inside the cwd after symlink resolution. It does not require the path to exist.
- Every rejection is one of a fixed set of reason codes. Arguments, paths, and OS error text are never echoed.

## Execution

- `asyncio.create_subprocess_exec` with an argv list. Never `shell=True`, never `sh -c`. Shell metacharacters that pass validation reach the program as literal argv elements.
- `stdin` is `/dev/null`. The child runs in a new session and process group (`start_new_session`).
- stdout and stderr are read with a shared hard byte cap. When it is exceeded the process group is stopped and `truncated` is set. Output is decoded with `errors="replace"`, and control characters are replaced with visible stand-ins, so the text is inert data. It never contains more characters than bytes.
- Timeout, cancellation, and output-cap stops send `SIGTERM` to the whole process group, wait a short grace period, then `SIGKILL` the group. After a normal exit any remaining members of the group are killed as well, so no background process outlives a run. `cleanup_complete` reports whether the group was confirmed gone.
- The tool's own timeout is always shorter than the registry's (command timeout plus a margin), so a timed-out run reports facts instead of being abandoned.

Output (always untrusted data): `command`, `exit_code` (`-1` when the process did not exit by itself), `signal` and `signal_name` (`0`/`""` if none), `timed_out`, `cancelled`, `truncated`, `stdout`, `stderr`, `stdout_bytes`, `stderr_bytes`, `duration_ms`, `cleanup_complete`.

## Exit code 0 is not success

The tool reports facts and nothing else. A command can exit 0 without achieving the goal, and a non-zero exit can still leave the desired state. Callers judge the real postcondition (a file exists, the repository state changed, a test report says what they expect) with a `ResultVerifier`: a callable `(ShellFacts) -> VerificationOutcome`. `verify_shell_result(tool_result, verifier)` returns:

- `UNVERIFIED` when no verifier is supplied, even for exit 0;
- `FAILED` when the tool result is not `ok`, the output is malformed, or the verifier raises or returns something else;
- otherwise the verifier's own `VerificationOutcome`.

`ShellFacts.exit_zero` only says "ended with status 0 and nothing was cut short"; it is not a verdict.

## Permission wiring and the second gate

`shell_scope_checks(tools)` returns scope predicates for `PermissionPolicy`. An unknown command, unknown cwd root, cwd escape, executable change, or argument-grammar violation is `out_of_scope` before execution, and no grant can override that. Red entries need the one-time, argument-digest-bound grant from the permission layer; a replayed or mismatched grant is refused.

The tool does not trust the policy. `ShellTool.run` re-validates everything itself: command lookup within its own permission level, cwd, arguments, executable identity, the context's tool name and level, and, for Red entries, `context.confirmed`. A failure raises `ShellRejected` with a fixed reason code; through the registry this surfaces as `internal_error`, and the exact reason is in the shell audit record. With correct wiring the second gate is never reached; it exists for misconfiguration and for races (a cwd or executable changed after the scope check).

The executable is re-checked on every run: the realpath of the registered path must equal the registered resolution, and the file's device and inode must match registration. A swapped symlink, a replaced file, or a lost exec bit refuses the run. Updating an interpreter in place (a new inode) therefore requires re-registering the command.

## Audit

`ShellAuditSink.record(ShellAuditRecord)` receives one record per run or refusal: call id, tool, command name, permission, outcome (`completed`, `nonzero_exit`, `signaled`, `timed_out`, `cancelled`, `output_cap`, `rejected`, `spawn_failed`, `internal_error`), reason code, whether a grant confirmed it, exit code and signal, a SHA-256 digest and count of the arguments, the cwd root name and a digest of the cwd, captured byte counts, `truncated`, `cleanup_complete`, and duration. It contains no stdout or stderr text and no argument values. This is separate from the registry's `ToolAuditRecord` (which also holds digests and sizes only) so that timeouts, cancellations, and non-zero exits, which are `ok` results at the registry level, are still recorded as such. Cancellation is recorded whether the token or the registry noticed it first. A failing sink is logged and does not change the outcome. The in-memory sink is bounded and for tests.

## Builders (not registered by default)

Both are Yellow and must be registered explicitly.

`git_readonly_command(executable, cwd_roots)`: `status`, `log`, and `diff` with a bounded flag set. It rejects `-c`, `-C`, `--exec-path`, `--upload-pack`, `--receive-pack`, `--git-dir`, `--work-tree`, `--output`, `--ext-diff`, `--textconv`, pagers, `--no-index`, and every other flag or subcommand it does not list, so there are no aliases or global options. The application pins config with trusted `-c` overrides (`core.fsmonitor=false`, pager and signature settings), `--no-pager`, `--no-optional-locks`, and fixed `GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_GLOBAL`/`GIT_PAGER` values; no `GIT_*` variable is inherited, so `GIT_EXTERNAL_DIFF`, `GIT_SSH_COMMAND`, and similar are dropped. `diff` and `log` get `--no-ext-diff --no-textconv` injected. Because its positionals are free-form, it can never be Green. "Read-only" means no write flags: git reading a repository whose own `.git/config` or `.gitattributes` you do not control can still run programs those files configure (filter drivers, for example).

`pytest_command(python_executable, cwd_roots)`: runs `python -m pytest -p no:cacheprovider --confcutdir=.` with a small flag set (`-q`, `-v`, `-x`, `--maxfail=N`, `-k`, `-m`, `--tb=…`, `--durations=N`, relative test paths). It rejects `-p`, `-c`, `-o`, `--rootdir`, `--confcutdir`, `--basetemp`, `--junitxml`, `--pdb`, `--pdbcls`, `--import-mode`, `--pyargs`, and anything else not listed. Plugin autoload is off unless `autoload_plugins=True`. **Warning: pytest imports and executes project code** (test modules, `conftest.py`, plugins named in the project's own configuration). The flag allow-list limits what the model can add; it does not sandbox the project. Register this command only for a project you would run yourself, keep its cwd root narrow, and treat the output as untrusted. Because the executable is resolved with `realpath`, a venv interpreter becomes its base interpreter and loses the venv's packages; register an interpreter that has pytest installed.

## Limits and residual risks

- Check-then-use races remain: a path validated by `RelPath` or as the cwd, or an executable verified before spawn, can be swapped by something with write access to those directories before the child uses it. Keep cwd roots and executable directories writable only by trusted users.
- A child that moves itself into a new session (`setsid`) escapes the process-group kill. Containers without an init that reaps orphans can report `cleanup_complete: false` because zombies still count as group members. Group-signalling relies on pgid reuse not happening in the instant between the leader's exit and the final kill.
- There are no CPU, memory, file-size, or network limits and no filesystem sandbox. The cap and timeout bound output and wall-clock time only. A permitted program runs with the server user's privileges.
- Process-group handling was exercised on macOS (Python 3.13). Linux and Python 3.11 behaviour is expected to match but is verified only by CI.
- Windows is unsupported and refuses to build.
- No network tools, no task queue, no UI for grants, and no persistent audit storage (see [tools.md](tools.md)).
