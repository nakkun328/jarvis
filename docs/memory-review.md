# Local memory review

Memory candidates remain `pending` or `conflict` until a person explicitly reviews
them. The local command below reads an existing JARVIS SQLite database. It does not
create or migrate one, and listing or showing candidates does not write a vault note.

```sh
python -m backend.memory.review_cli --db data/jarvis.sqlite3 list
python -m backend.memory.review_cli --db data/jarvis.sqlite3 list --status conflict
python -m backend.memory.review_cli --db data/jarvis.sqlite3 show MEMORY_UUID
```

`list` returns candidate IDs and provenance without the full content. `show` returns
the full candidate and its review state as JSON. Review its source, whether it is a
user statement or an AI inference, and any existing related memory before choosing
an action. A conflict is not an approved fact.

```sh
python -m backend.memory.review_cli --db data/jarvis.sqlite3 flag-conflict MEMORY_UUID
python -m backend.memory.review_cli --db data/jarvis.sqlite3 reject MEMORY_UUID
python -m backend.memory.review_cli --db data/jarvis.sqlite3 approve MEMORY_UUID --vault /path/to/Obsidian/JARVIS
```

`approve` uses `MemoryWriter`: it creates or verifies the UUID note in the editable
vault, then records the note revision and approved state in SQLite. It never
overwrites a different existing note. A candidate may be approved from `pending` or
`conflict`; `reject` applies to either state. Approved and rejected candidates are
terminal under the current repository rules. A correction must be staged as a new
candidate. If a command fails, inspect the candidate and vault note before retrying.

Run this command only on the local machine with access to the database and vault.
It provides no network endpoint or authentication and must not be exposed as a
remote service. The database and vault may contain private information; avoid
redirecting `show` output to a shared log.
