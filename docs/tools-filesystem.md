# Read-only filesystem tools

Phase 4 / T2 (JAR-71), first slice. `backend/tools/filesystem.py` adds three **Green** tools on top of the contract in [tools.md](tools.md): `fs.list`, `fs.read_text`, and `fs.search`. There is no write, move, delete, shell, or network code in this module; those need separate, higher permission levels and a design for previews and recovery.

## Roots are application configuration

```python
from backend.tools.filesystem import FilesystemRoot, filesystem_scope_checks, register_readonly_filesystem_tools

roots = [FilesystemRoot("notes", notes_directory)]
policy = PermissionPolicy(scope_checks=filesystem_scope_checks(roots))
registry = ToolRegistry(policy, audit=sink)
register_readonly_filesystem_tools(registry, roots)
```

A `FilesystemRoot(name, path, allow_denied_paths=())` must be an absolute path to an existing directory. It is canonicalised (symlinks resolved) on creation and its device and inode are recorded; a root that is later replaced is refused. The model never supplies a path to a root, only the root's short name (`[a-z][a-z0-9_-]{0,31}`, at most 32 roots) and a path relative to it. Root names are listed in each tool description; nothing else about a root is exposed. An unknown name is refused.

## Tools

| Tool | Arguments (all strings/ints; extra keys rejected) | Result |
| --- | --- | --- |
| `fs.list` | `root`, `path` (default root), `max_depth` 1-3 (default 1), `max_entries` 1-1000 (default 200) | `entries` of `path`, `name`, `type` (`file`/`directory`/`symlink`/`other`), `size` (0 unless a file), `mtime` (UTC ISO-8601), sorted by case-folded name; `truncated`. |
| `fs.read_text` | `root`, `path`, `offset` (bytes, default 0), `length` (bytes, 4 to 65,536, default 32,768) | `text`, `offset`, `bytes_read`, `next_offset`, `size`, `truncated`. |
| `fs.search` | `root`, `path`, any of `name_contains`, `glob`, `content`; `case_sensitive`, `max_depth` 1-8 (default 4), `max_matches` 1-200 (default 50) | `matches` (`path`, `kind` `name`/`content`, `line`, `snippet` up to 200 characters), `entries_visited`, `bytes_read`, `files_skipped`, `files_partially_searched`, `truncated`, `stop_reason`. |

Details:

- `fs.read_text` decodes UTF-8 strictly: no replacement characters, a BOM stays a character, CRLF is preserved. A file with a NUL byte in the window or in its first 8 KiB, or with invalid UTF-8, is refused. `offset` must fall on a character boundary; the window is trimmed back to a whole character, so continue from `next_offset`. The 65,536-byte cap keeps decoded text inside the contract's string limit.
- `fs.search` combines the given filters with AND. `content` is a literal substring (no regex) matched per line. Only the first 256 KiB of each file is searched (`files_partially_searched` counts the rest). Caps: 10,000 entries visited, 4 MiB read in total, 200 matches, and 5 seconds; `stop_reason` is `complete`, `max_matches`, `entry_limit`, `byte_limit`, or `time_limit`, and a capped search returns what it found with `truncated: true`.
- `fs.list` and `fs.search` skip names the tools could not address again (control characters, `:`, `\`, trailing dots or spaces, Windows device names, over-long components). `fs.list` shows a symlink as an entry of type `symlink` but never follows it or reports its target. Directories with more than 10,000 entries are scanned only up to that count and flagged `truncated`; which entries make the cut is then not defined.
- The `FilesystemLimits` object (application side) lowers or raises the defaults within hard maxima; the schema bounds always apply.

## Safety contract

1. **Lexical checks** (`parse_relative_path`, no filesystem access): the path must be a string of at most 512 characters; absolute paths, `..` and `.` components, empty components, NUL and any control, format, or separator character (including bidi overrides and zero-width characters), backslashes, `:` (drive letters, NTFS streams), leading or trailing spaces, trailing dots, Windows device names, and components over 255 characters or bytes are refused. `~` and `$VAR` are ordinary names and are never expanded.
2. **Deny-list.** By default these names are refused wherever they appear in the path, matched case-insensitively (NFC and case-fold): `.git`, `.ssh`, `.aws`, `.gnupg`, `.kube`, `.azure`, `.env`, `.env.*`, `.netrc`, `_netrc`, `.npmrc`, `.pypirc`, `.pgpass`, `.git-credentials`, `.htpasswd`, `id_*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx`, `credentials`, `credentials.json`. The whole `.git` directory is denied, not just `.git/config`. The check happens before the filesystem is consulted, so a refusal does not reveal whether the file exists, and denied entries are left out of listings and searches without a count. The list is deliberately coarse: it also hides, for example, Keynote `.key` files and anything named `id_*`.
   The only override is `FilesystemRoot(allow_denied_paths=...)`: exact root-relative paths (for example `config/.env.example`) that the application chooses to expose. It is not a tool argument, cannot be a pattern, and does not extend to other files in the same directory.
3. **Containment.** The path is resolved with `realpath` and must stay inside the canonical root and contain no symlink. In this slice **every symlink is refused**, including a link that points inside the root; that is simpler and stricter than following links with a re-check.
4. **Descriptor-relative access.** The path is then walked one component at a time with `openat`-style calls (`dir_fd`) and `O_NOFOLLOW`, so a component swapped for a symlink after the checks is refused instead of followed. Each directory must be on the root's device (a mount point inside the root is refused, like `find -xdev`). A file is `lstat`-checked before it is opened, so a FIFO, device node, or socket is refused without ever being opened (no blocking); it is opened with `O_NOFOLLOW | O_NONBLOCK | O_NOCTTY`, and `fstat` must report the same regular file (device and inode) as the `lstat`.
5. **Fail closed.** If the platform lacks `O_NOFOLLOW`, `O_DIRECTORY`, `dir_fd`, or `scandir(fd)` (for example Windows), the tools refuse to be built (`tool_unavailable`) instead of falling back to path-based access.
6. **Untrusted output.** File names and text are returned verbatim as data. Tool descriptions say so, and nothing in the tools reads text as instructions: file content cannot change a permission, a policy, the set of tools, or the control flow. A test returns a prompt-injection-style body byte for byte and verifies that nothing else changes.
7. **Errors.** Failures raise `FilesystemToolError` whose message is a fixed reason code (`not_found`, `denied_name`, `outside_root`, ...) with no path and no OS error text, and `.code` is the `ToolErrorCode` it maps to (`invalid_arguments`, `permission_denied`, `cancelled`, `internal_error`, ...). The registry currently reports every exception raised by a tool as `internal_error` (see "Maintainer decisions"), so through the registry the model only learns that the call failed.
8. **Audit.** The registry's audit records carry digests and sizes of the arguments and output only. File contents, file names, and paths never enter them (tested).

### Permission policy

The tools are Green, so the default policy allows them. `filesystem_scope_checks(roots)` returns `PermissionPolicy` scope predicates for the three tools: an unknown root, a malformed path, or a deny-listed name is refused by the permission layer (`out_of_scope`, `permission_denied`) before the tool runs, and no confirmation grant can override a failed scope check. The tools enforce the same rules themselves, so they stay safe when registered without the predicates. Deny-listing a tool name in the policy disables it as for any tool.

## Known limits and residual risk

- **TOCTOU.** The walk is race-resistant for symlink swaps (tested by simulating a swap between check and open), but not race-free. Residual risks: the root directory itself and the ancestors of its canonical path are trusted (the root is opened by path with `O_NOFOLLOW` on the last component and compared with its recorded device and inode; a swap of an ancestor directory between configuration and use by someone who can write there is outside this protection); a file's content can change between the `lstat`, the `open`, and the reads, so a window may mix versions; entries can appear or vanish during a scan, and a swapped file makes the call fail with `internal_error` rather than return mixed data where it is detected. Anyone who can write inside a root concurrently can cause refusals and torn reads, but, within the checks above, not escapes through symlinks.
- **Hard links.** A hard link inside the root to a file outside it (or to a denied file under another name) is an ordinary file for these tools; the deny-list is name-based. Treat the people and programs that can write inside a root as trusted. On Linux, `fs.protected_hardlinks` limits creating such links to files the user owns or can write.
- **Case-insensitive filesystems.** Deny-list matching is case-folded, so `.ENV` is refused whatever the filesystem does. Symlink detection compares `realpath` output with the requested path without changing case, and the `O_NOFOLLOW` walk does not depend on case. The test suite was only run on macOS; no test deliberately targets a case-insensitive or Unicode-normalising volume (the tests avoid names that differ only by case so they pass on both kinds), so behaviour on such volumes beyond the deny-list folding is not verified, and case-collision or normalisation tricks are not specifically defended.
- **Mounts and special filesystems.** Crossing a device boundary inside the root is refused; bind mounts on the same device are not detected. `/proc`-style filesystems are only reachable if the application chooses one as a root.
- **Not covered:** reading files the OS user can read but the application should not expose beyond the deny-list (choose roots narrowly), file ownership and ACLs, Windows support, and a time limit that can interrupt a single blocking filesystem call (a hung network filesystem can still stall a worker thread until it returns).
- Thread usage: each call runs its blocking work in a worker thread. After a timeout or cancellation the thread notices at the next entry, and its work is bounded by the same caps.

## Maintainer decisions needed

- Whether to extend the registry so a tool can raise an error that carries a `ToolErrorCode` (for example `ToolFailure(code)`), so `invalid_arguments` and `permission_denied` reach the caller instead of `internal_error`.
- Whether the default deny-list is the right size, and which exact paths an installation should add to `allow_denied_paths`.
- Whether in-root symlinks should ever be followed, and whether files with more than one hard link should be refused.
- How roots are configured (settings, environment, per-device) and whether `fs.list` should offer a way to list root names instead of relying on the tool description.
- Write, move, and delete tools: permission levels, previews, backups and recovery (T2 later slices).
