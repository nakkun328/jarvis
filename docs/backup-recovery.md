# SQLite/vault backup and recovery

SQLite owns conversation transcripts, candidate status, review/lifecycle history,
and correction links. The current approved Obsidian note owns editable memory
content. Chroma is derived and can be rebuilt. Back up SQLite and the entire
vault as one operational snapshot; preserving only approved notes loses history
and pending publication recovery material.


## Availability and prerequisites

Current state checked on 2026-10-03: main
`ce0118c9bb8958e055ecae46823f82a4fda2cb61` includes landed PRs #30, #23, #35 and #33.
SQLite schema v5 preserves conversations, candidates, approvals, review and
lifecycle history, and replacement/revision links. Initialization migrates
supported older schemas; the local review CLI requires an existing ready DB.
Explicit corrections, supersession and retirement are now available on main.
Canonical retrieval excludes inactive memories immediately; it does not
physically delete their cached vectors.
Derived rebuild/refresh/audit APIs require
[PR #18](https://github.com/nakkun328/jarvis/pull/18),
[PR #21](https://github.com/nakkun328/jarvis/pull/21),
[PR #24](https://github.com/nakkun328/jarvis/pull/24),
[PR #26](https://github.com/nakkun328/jarvis/pull/26) and
[PR #29](https://github.com/nakkun328/jarvis/pull/29).
Opt-in chat memory is now on main: `JARVIS_MEMORY_VAULT_PATH` defaults to unset,
and an existing vault explicitly enables bounded approved lexical references.
References are rechecked before the provider call and are not transcript turns.
Shared provider cleanup is also on main: explicit stream close, SSE transport
failure/disconnect/cancellation, startup failure and shutdown release owned
resources. Iterators without `aclose` remain supported. Failed or interrupted
partial streams do not save conversation turns; successful responses do.
Gemini remains pending #31. Use a matching reviewed code version; this runbook
does not make pending index or Gemini changes available or authorize a merge.
No live API is needed for disposable validation. OpenAI live validation remains pending.
The verification gate is now on main: install the dev/vector dependencies and
use [the gate procedure](verification-gates.md) from a clean checkout. GitHub CI
records the actual checkout SHA separately from a PR source head and retains
the terminal receipt and logs. Gate success does not replace inspection of a
restored snapshot or establish live API/model quality.

## Back up a consistent snapshot

1. Stop the one-worker app, consolidation/review jobs, and external vault
   editors/sync writers. Wait for requests and jobs to finish. Keep them stopped
   until both resources have been copied. SQLite's online backup API gives a
   consistent DB snapshot but does not make SQLite and a filesystem vault one
   atomic transaction. This quiescence requirement closes that gap.
2. Choose a new private destination. Keep the configured source DB/vault paths
   and code commit SHA with the backup. Do not copy API keys or `.env` files.
3. From the checked-out repo and its installed Python environment, run the code
   below after substituting the three absolute paths. It refuses existing
   destinations and missing DBs, uses SQLite `backup()` (including committed WAL
   data), copies the vault, checks DB integrity/FKs, and writes a completion
   manifest only when all copies are finished.

```sh
python - /absolute/path/memory.sqlite3 /absolute/path/vault /absolute/path/new-backup <<'PY'
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

db, vault, snapshot = map(Path, sys.argv[1:])
if not db.is_file() or db.is_symlink() or not vault.is_dir() or vault.is_symlink():
    raise SystemExit("Expected a regular source DB and vault directory")
if any(p.is_symlink() for p in vault.rglob("*")):
    raise SystemExit("Inspect vault symlinks before backing up")
snapshot.mkdir(mode=0o700, parents=False, exist_ok=False)
target = snapshot / "memory.sqlite3"
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as source:
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SystemExit("Backup integrity check failed; snapshot is incomplete")
        if destination.execute("PRAGMA foreign_key_check").fetchall():
            raise SystemExit("Backup foreign key check failed; snapshot is incomplete")
        version = destination.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        user_version = destination.execute("PRAGMA user_version").fetchone()[0]
        history = list(destination.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version"))
shutil.copytree(vault, snapshot / "vault", symlinks=False)
for directory in [snapshot / "vault", *(p for p in (snapshot / "vault").rglob("*") if p.is_dir())]:
    directory.chmod(0o700)
for file in (snapshot / "vault").rglob("*"):
    if file.is_file():
        file.chmod(0o600)
checksums = {
    p.relative_to(snapshot).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in snapshot.rglob("*") if p.is_file()
}
manifest = {"schema_version": version, "sqlite_user_version": user_version, "migration_history": history,
            "code_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "sha256": checksums, "quiesced_snapshot": True}
completion = snapshot / "snapshot.json"
completion.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
completion.chmod(0o600)
print("Completed SQLite and vault snapshot")
PY
```

If the command fails, the destination is incomplete: inspect the error and use
another new destination after repair. A completion manifest is required before
restoring. This procedure is for a controlled local operation, not an automated
crash-consistent backup service. Keep the private backup off the active paths.

## Restore into new paths and verify

Keep app/jobs/editors stopped. Never overwrite a running SQLite DB or open Chroma
directory. Verify backup checksums, copy into a new private restore directory,
and verify the restored DB before changing any active configuration.

```sh
python - /absolute/path/completed-backup /absolute/path/new-restore <<'PY'
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
snapshot, restored = map(Path, sys.argv[1:])
manifest = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
if manifest.get("quiesced_snapshot") is not True:
    raise SystemExit("Missing consistent-snapshot marker")
for relative, expected in manifest["sha256"].items():
    file = snapshot / relative
    if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
        raise SystemExit("Backup checksum mismatch; stop recovery")
shutil.copytree(snapshot, restored, symlinks=False)
restored.chmod(0o700)
database = restored / "memory.sqlite3"
database.chmod(0o600)
with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA user_version").fetchone()[0] == manifest["sqlite_user_version"]
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == manifest["schema_version"]
    assert list(connection.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")) == [tuple(r) for r in manifest["migration_history"]]
print("Restored snapshot verified; active configuration has not been changed")
PY
```

Use the code version recorded in the manifest, or inspect its supported schema
first. Application initialization migrates supported old schema versions;
review CLI commands require an already prepared DB and do not initialize or
migrate one. Retain the original snapshot when testing migration. Do not try to
downgrade a newer schema or claim a nonempty unversioned DB.

Inspect representative conversation turns, review history and correction links
using the repository API/CLI on restored paths. An approved missing/corrupt note
requires repair from the matching vault snapshot or review; do not substitute
the stale candidate text from SQLite or automatically reapprove content. Current
human edits remain canonical when their provenance is valid.

## Rebuild derived Chroma and restart

This section requires the pending embedding/index PRs listed above; its
physical cleanup APIs are not on current main. SQLite/vault backup and restore
from the preceding sections are available with main schema v5.

Follow the [index rebuild procedures in PR #21](https://github.com/nakkun328/jarvis/pull/21) with the restored SQLite repository,
restored vault, the caller's explicitly configured embedding provider/space, and
a **new private Chroma directory**. Run `populate_empty`, then `audit_ids` and
representative retrieval. Audit success requires exact current approved IDs and
note revisions. Pending, conflict, rejected, superseded and retired records must
not appear as approved retrieval matches.

The application has no automatic active-index switch or built-in production
embedding provider selection. Keep that configured caller pointed at the old
index until the new one verifies, then select the restored paths/new index and
restart one local worker. `JARVIS_DB_PATH` configures the app DB. On current main,
`JARVIS_MEMORY_VAULT_PATH` opts chat into an existing restored vault; unset it
to leave memory references disabled. Verify approved/corrected/retired reference
behavior and conversation continuity before resuming writers. Chat uses lexical
matching; semantic query embeddings and index selection remain caller-wired
pending APIs, not an automatic chat search or active-index switch.

For a failed reviewed publication, repair the cause and retry
`publish_reviewed(id, actor=...)`: a matching previously created note and one
committed approval are reused. `IndexRefreshError` means canonical approval was
committed and only the derived refresh/cleanup needs retry. For retirement use
`retire_reviewed(id, actor=..., reason=...)`; direct `MemoryWriter.retire` is not
an idempotent cleanup retry. `remove_inactive(id)` checks removal from every
stored embedding space. Inspect `audit_ids` after repair; a failed fresh build
directory should be abandoned for another new directory.

## Verified disposable exercise and limits

The external fault harness creates only temporary fabricated records, uses fake
embedding, and writes actual SQLite/vault/Chroma data. It opens a WAL connection,
uses `source.backup(target)`, copies the quiesced vault, restores into new paths,
and starts a fresh interpreter. It verifies conversation Japanese text,
supersession/replacement links and revisions, approval/lifecycle history,
terminal-state exclusion, a fresh Chroma rebuild/audit and a second interpreter
restart of the restored index. Fake embeddings demonstrate the retrieval and
integrity path; they do not validate model semantic quality. Sudden machine power
loss and multi-worker ordering are outside that process-restart exercise.

## Limits of the verified flow

Chat memory currently uses bounded lexical matching of approved vault notes;
semantic search is a separately called API and is not wired into chat. Fake
embedding cannot establish Japanese or model semantic quality. The current
extractor only handles labelled `Remember topic: ...` statements and typed Self
Memory events. Free-form extraction, remote access, authentication, automatic
index switching and multi-worker coordination are not part of these procedures.

A provider failure or interrupted stream does not save a partial successful turn.
After an error, retry the user request; after receiving a successful result, a
manual repeated request is a new turn because there is no request idempotency key.
Memory references are request context and are not stored as conversation turns.
Old facts explicitly supplied in successful user turns remain in conversation
history after long-term memory retirement; retirement governs reviewed memory
retrieval, not transcript deletion.

## Dated verification history

- 2026-10-03, initial Draft #32 at `3e97c3d`: backed-up code was main schema v4;
  lifecycle/index/chat methods required pending PRs. The disposable full
  integration verified SQLite backup, vault restoration and new Chroma rebuild.
- 2026-10-03, after #30 merge: main is schema v5 with reviewed lifecycle and
  canonical inactive exclusion. Concrete all-space cache cleanup/rebuild is
  still pending in the index stack, notably #29. Do not infer physical vector
  removal from a successful retirement on main. The main and integration
  checks remain separate evidence.

- 2026-10-03, after #23 merge: main `e5e0314` adds opt-in bounded lexical chat
  references. Correction approval changes the current reference and retirement
  excludes it; neither operation deletes successful historical conversation turns.
  Gemini and derived-index physical cleanup remain pending. Shared provider/SSE
  cleanup is being reviewed separately and must not be assumed landed.

- 2026-10-03, after #35 merge: main `d408e49` includes the shared resource
  cleanup described above. The preceding #23 entry records the earlier state.
  Gemini #31 and derived-index features remain pending; backup/restore code
  snippets are unchanged.

- 2026-10-03, after #33 merge: main `ce0118c` includes the verification runner,
  redacted Secret scan and Linux CI receipts. SQLite/vault recovery code and
  the backup/restore snippets are unchanged. Recovery tests #34, this runbook
  #32, Gemini #31 and the derived-index stack remain pending.
