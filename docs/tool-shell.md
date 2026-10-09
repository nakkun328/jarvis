# Shell tool: policy, settings and wiring (JAR-72)

The structured shell mechanism (argument grammar, no `sh -c`, scrubbed environment, timeout and output cap) is described in [tools-shell.md](tools-shell.md). This page covers what an installation may actually turn on. **Nothing is registered by default.** The shell is off unless `JARVIS_SHELL_ENABLED` is true *and* `JARVIS_SHELL_ROOT` is a usable directory.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JARVIS_SHELL_ENABLED` | `0` | Master switch. |
| `JARVIS_SHELL_ROOT` | unset | Absolute directory every command runs inside (its only cwd root, named `work`). |
| `JARVIS_SHELL_COMMANDS` | `ls,cat,git` | Names to enable from the allowlist table. `pytest` is in the table but only on when named here. |
| `JARVIS_SHELL_TIMEOUT_SECONDS` | `30` | Per-command wall-clock limit, 1 to 600. |
| `JARVIS_SHELL_MAX_OUTPUT_BYTES` | `32768` | Combined stdout+stderr cap, 1 to 65536. |

Invalid values stop startup like other settings (`ConfigError`). The root is rejected (the shell stays off) when it is relative, missing, not a directory, `/`, the home directory, or a parent of the home directory.

## Allowlist table

| Command | Level | Allowed |
| --- | --- | --- |
| `ls` | Green | Flags `-1 -l -a -A -h -F -t -r` only. No path arguments: it lists the call's working directory. |
| `cat` | Yellow | `-n` and up to four relative file paths inside the root. Refused: symlinks leaving the root, directories, `.git`, `.ssh`, `.aws` and similar, `.env*`, `*.pem`, `*.key`, `id_*`, names containing secret/credential/password/token (also when reached through a symlink). |
| `git` | Yellow | `status`, `log`, `diff` with a bounded flag set; no `-c`, `-C`, `--git-dir`, `--output`, external diff, `--no-index`, pager, upload/receive pack. |
| `pytest` | Yellow | `python -m pytest` with a bounded flag set. Runs project code: enable only for projects you would run yourself. |

Policy is data (`DEFAULT_ALLOWLIST` in `backend/tools/shell_policy.py`): each row names the level, candidate executables and a builder. A row cannot be Red, and the built command must match the declared level and name. Executables are fixed absolute paths, never looked up on the model's `PATH`. Unknown names in `JARVIS_SHELL_COMMANDS` make the doctor report INCOMPLETE and register nothing.

Green is reserved for commands whose whole argument grammar is closed. Yellow is never auto-allowed: `build_shell_wiring` refuses a base policy that lists a shell tool in `allow_yellow`, so every Yellow call needs a human approval through [tool-confirmation.md](tool-confirmation.md). `git` stays Yellow even though it is read-only by flags: repository config can still make git run programs.

## Wiring

`build_shell_wiring(settings, approvals=...)` returns `None` (and logs only a fixed reason code) unless the shell is enabled with a valid root and at least one allowlisted executable exists. Otherwise it returns a private `ToolRegistry` holding `shell.run_readonly` (Green) and/or `shell.run` (Yellow), with the shell scope checks in its permission policy, and a `ConfirmedExecutor` over it. Callers run shell calls through that executor; there is no other entry point. An optional `base_policy` is merged (deny list and scope checks are kept).

Nothing in the application calls the factory yet; hooking it into the chat/agent loop is a separate step.

## Doctor

`python -m backend.doctor` has a `shell` row: `OFF` (disabled), `OK` (ready, lists command names) or `INCOMPLETE` with one of `SHELL_ROOT_MISSING`, `SHELL_ROOT_INVALID`, `SHELL_COMMAND_UNKNOWN`, `SHELL_NO_COMMANDS`, `SHELL_UNSUPPORTED`. It never prints the root path.

## Audit

Each run or refusal that reaches the tool writes one `ShellRunAudit` to a `ShellRunSink` (default: a JSON line on logger `jarvis.shell.audit`): time, call id, tool, command name, a **redacted argv summary** (at most six arguments of 48 characters, credential-looking values and values after secret-looking flags replaced, control characters removed), permission level, whether a human confirmed it, outcome and rejection reason, exit code, signal, duration, and whether output was truncated. Output content and resolved paths are never recorded. Calls refused by the scope check (hostile arguments) never reach the tool, so they appear only in the registry's own audit (`tool_audit`), as digests.

## Approval screen

The approval summary now shows short all-string lists and objects (the shell `args` and `cwd`) so the human sees what they approve, for example `[-n, a.txt]`. Lists or objects containing credential-looking values or secret-named keys still show only type and size. The approval stays bound to the full argument digest.

## Not covered

- Arguments are validated against the tree at request time; a symlink swapped between validation and execution is a residual race (see tools-shell.md).
- No chat/agent integration, no UI to toggle the shell, no persisted audit table (logger only).
- No per-person identity for approvers.
