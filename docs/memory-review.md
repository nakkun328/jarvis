# Local memory review

Memory candidates remain `pending` or `conflict` until a person explicitly reviews
them. The local command below reads an existing JARVIS SQLite database. It does not
create or migrate one, and listing or showing candidates does not write a vault note.

```sh
python -m backend.memory.review_cli --db data/jarvis.sqlite3 list
python -m backend.memory.review_cli --db data/jarvis.sqlite3 list --status conflict
python -m backend.memory.review_cli --db data/jarvis.sqlite3 show MEMORY_UUID
python -m backend.memory.review_cli --db data/jarvis.sqlite3 history MEMORY_UUID
```

`list` returns candidate IDs and provenance without the full content. `show` returns
the full candidate and its review state as JSON. `history` returns committed review
and lifecycle events with their actor labels. Review its source, whether it is a
user statement or an AI inference, and any existing related memory before choosing
an action. A conflict is not an approved fact.

```sh
python -m backend.memory.review_cli --db data/jarvis.sqlite3 flag-conflict MEMORY_UUID --actor alice
python -m backend.memory.review_cli --db data/jarvis.sqlite3 reject MEMORY_UUID --actor alice
python -m backend.memory.review_cli --db data/jarvis.sqlite3 approve MEMORY_UUID --vault /path/to/Obsidian/JARVIS --actor alice
python -m backend.memory.review_cli --db data/jarvis.sqlite3 correct OLD_UUID --vault /path/to/Obsidian/JARVIS --content-file /private/correction.txt --source user:correction --origin user_explicit
python -m backend.memory.review_cli --db data/jarvis.sqlite3 approve NEW_UUID --vault /path/to/Obsidian/JARVIS --actor alice
python -m backend.memory.review_cli --db data/jarvis.sqlite3 retire MEMORY_UUID --vault /path/to/Obsidian/JARVIS --actor alice --reason "No longer current"
```

`approve` uses `MemoryWriter`: it creates or verifies the UUID note in the editable
vault, then records the note revision and approved state in SQLite. It never
overwrites a different existing note. A candidate may be approved from `pending` or
`conflict`; `reject` applies to either state. `correct` stages a separate pending
candidate linked to the currently approved note and its observed revision. The
old note stays active until `approve NEW_UUID` atomically approves the replacement
and marks the old row `superseded`. Editing the old note after staging blocks the
stale correction. `retire` explicitly removes an approved fact from retrieval
without deleting its vault note. `history` shows review and lifecycle audit events;
`show` includes replacement IDs. If a command fails, inspect the candidate and
vault note before retrying.
The required `--actor` label is supplied by the local operator and recorded with
each successful state transition. It is not an authenticated identity. Repeating
approval of an already approved note does not create another transition event.

CLI correction and retirement do not have a vector index adapter. Their SQLite
states immediately exclude inactive IDs from canonical retrieval, even when the
derived index still holds stale entries. Run index audit and rebuild after these
commands to remove stale vector entries. `MemoryConsolidator.publish_reviewed` and
`retire_reviewed` can use an `IndexRefresher` adapter for immediate cleanup; a
failure preserves the SQLite decision and exposes a retry method. SQLite and
human-editable vault files cannot share one atomic transaction; changes to an old
note between final vault verification and the SQLite commit need a later audit.

Run this command only on the local machine with access to the database and vault.
It provides no network endpoint or authentication and must not be exposed as a
remote service. The database and vault may contain private information; avoid
redirecting `show` output to a shared log.
