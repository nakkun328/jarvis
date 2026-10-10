# Filesystem write tools

Range-limited write tools built on the read tools' path safety ([tools-filesystem.md](tools-filesystem.md)) and gated by human approval ([tool-confirmation.md](tool-confirmation.md)). **Nothing registers them by default.** Application code must call `register_filesystem_write_tools(registry, roots, ...)` (module `backend/tools/filesystem_write.py`) with explicit roots; a test asserts no other backend module references the module.

## Tools

| Tool | Level | Behaviour |
| --- | --- | --- |
| `fs.write_file` | Red | Create a text file; replaces an existing one only with `overwrite=true`. Atomic: temp file in the same directory, fsync, then `link` (create) or `rename` (overwrite). Parent must exist. New files are mode 0600; overwrite keeps the old mode. |
| `fs.append` | Yellow | Append to an existing regular, singly-linked file. Result must stay under the per-file cap. Not atomic. |
| `fs.make_dir` | Yellow | Create a directory (mode 0700); `parents=true` creates at most 8 missing parents. Fails if it exists. |
| `fs.move` | Yellow | Move or rename a file, directory or symlink inside one root. Never replaces a destination; refuses moving a directory into itself. |
| `fs.delete` | Yellow | "Delete" = move into `.jarvis-trash/` at the top of the root under `<UTC time>-<random>-<name>`. Nothing is ever physically removed. |

All of them require a human approval: the policy demands it (Yellow/Red are not allow-listed by default) and each tool also refuses to run when the registry did not accept a grant (`context.confirmed`), so adding one to `allow_yellow` does not make it unattended.

## Content is staged, not passed

The call arguments never contain file text. Application code puts the text in a `ContentStage` (bounded entries and bytes, 15 minute expiry, UTF-8 text without NUL) and builds the call from `content_sha256` and `content_size`. Therefore:

- the approval digest covers root, path, `overwrite`, the content hash and size, so a different text, path, root or `overwrite` needs a new approval;
- the approval summary and database row hold only the path, hash (shown redacted) and size, never the text;
- the tool re-hashes the staged bytes and checks the size before writing; a missing, expired, resized or tampered entry fails with `content_unavailable`. A staged entry is dropped after a successful write.

## Path rules

Same as the read tools (relative paths only; no `..`, absolute paths, control characters, backslashes, colons, trailing dots/spaces, Windows device names; credential-shaped names denied case-insensitively with NFC + casefold; realpath containment; descriptor-relative walk with `O_NOFOLLOW`; mount points and a replaced root are refused). Extra, non-overridable write rules:

- denied names (every component, case-insensitive): `*.sqlite`, `*.sqlite3`, `*.db` and their `-wal`/`-shm`/`-journal` siblings, `vault`/`vault.*`/`*.vault`/`.vault*`, `.obsidian`, `secret(s)` and `*.secret(s)`, the trash directory and the tools' own temp names (`.jarvis-tmp-*`). `extra_denied_patterns` lets the application add more;
- `allow_denied_paths` on a root exempts only the read tools' credential list, never the write list;
- `filesystem_write_scope_checks` (also returned in the toolset) plugs the same lexical checks into `PermissionPolicy(scope_checks=...)`, so a denied path is refused as `out_of_scope` and never creates an approval request;
- caps: 256 KiB per write by default (hard cap 1 MiB), 1 MiB per file after an append.

## Race and symlink behaviour

- Symlinks are never followed: a symlinked directory component or file is refused; an existing symlink is never written through.
- If a target is swapped for a symlink after the check, `rename` replaces the link itself and the link target is untouched; `append` opens with `O_NOFOLLOW` and fails.
- `create` uses `link`, which cannot clobber a file a racer created meanwhile (`already_exists`).
- Overwriting a hard-linked file replaces the name, so the other link keeps its old content; `append` refuses files with more than one link because it writes in place.
- `move` of files is `link` + `unlink` with an inode check so a swapped source is not deleted. Directory moves use `rename` after an existence check; a destination that appears as an empty directory in that window could be replaced. Case-only renames on case-insensitive filesystems report `already_exists`.
- A crash between temp write and publish can leave a `.jarvis-tmp-*` file. POSIX only (needs `dir_fd`/`O_NOFOLLOW`); other platforms report `tool_unavailable`.

## Not covered

- Restoring from trash and emptying it: no tool addresses the trash, a human does it.
- Binary content, file permissions changes, cross-root moves, and delete-by-physical-removal.
- Wiring into chat: nothing stages content or registers these tools yet.
