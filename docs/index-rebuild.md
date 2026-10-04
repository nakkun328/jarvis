# Rebuild the derived memory index

`MemoryIndexBuilder.populate_empty` reads every approved memory from SQLite and
the **current** Obsidian note, validates provenance, generates embeddings with a
caller-supplied `EmbeddingProvider`, and writes IDs, vectors, and the current
note revision hash to an empty index space. It verifies that indexed IDs and
revisions exactly match the approved canonical set before reporting success.
Pending, conflicted, and rejected
candidates are never embedded by this builder.

The builder re-reads the full approved set immediately before and after writing.
It aborts if a note revision changed or a new memory was approved during that
window. A review operation after the final check may still make the derived
index lag; run `audit_ids` and refresh or rebuild before switching a reader.

Use a **new, private Chroma directory** for each rebuild. The builder refuses an
already populated space so stale IDs from an earlier build cannot survive. It
does not switch an active reader to the new directory. Keep the old index until
the new count, IDs, and representative searches have been checked, then change
the reader's configured path in a separate operation. Canonical SQLite and vault
content are unaffected if a build fails; discard or inspect the failed derived
directory before trying a new one.

```python
from pathlib import Path

from backend.memory.chroma import ChromaVectorIndex
from backend.memory.indexing import MemoryIndexBuilder
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.retrieval import MemoryRetriever

repository = MemoryRepository(database)
vault = ObsidianVault(vault_path)
index = ChromaVectorIndex(Path("data/vectors-next"))  # a new empty directory
builder = MemoryIndexBuilder(
    repository,
    MemoryRetriever(repository, vault),
    embedding_provider,  # explicitly configured by the caller
    index,
)
report = await builder.populate_empty()
assert report.count == len(report.memory_ids)
```

The example assumes the caller has created `database`, `vault_path`, and a
provider implementing the versioned embedding contract. A remote provider may
receive private memory text; choose and configure it deliberately. No provider
or API key is selected by this module. Changing the model, text preparation, or
dimension requires a new `EmbeddingSpace` version and rebuild. Human edits after
the build may make vectors stale. After an explicitly reviewed change, a caller
can run `await builder.refresh_approved(memory_id)` to replace that one vector
from the current note. Retrieval must still resolve every result against current
SQLite and vault state. The read-only audit detects edits when run; no watcher
automatically triggers it. Active-index switching remains a caller operation.

`await builder.audit_ids()` is a read-only operational check. It validates
every approved current vault note, then reports approved IDs missing from the
configured index space, extra indexed IDs, `stale_ids` whose indexed note hash
differs from the current note, and `untracked_ids` indexed before revision
tracking. A missing or invalid approved note raises an error instead of
producing a reassuring report. The canonical set is read again after inspecting
the index; a change during the audit also raises instead of reporting healthy.
`healthy` requires all four lists to be empty.
This detects human edits and unknown legacy revisions, but cannot prove that
the embedding provider generated a useful vector. Refresh an untracked ID or
rebuild the space before treating it as current.

## Refresh after explicit review

For an offline synchronous consolidation job, pass
`SynchronousIndexRefresher(builder)` to `MemoryConsolidator` as its
`index_refresher`. `publish_reviewed` then re-reads the approved current note,
embeds it in the builder's configured space, and upserts its ID and vector.
The adapter rejects a note whose revision changed between publication and
refresh. If embedding or Chroma fails, approval and the vault note remain
durable; `publish_reviewed` reports `IndexRefreshError`. After repairing the
cause, call `publish_reviewed` again to retry without creating another note.
Run this synchronous path from a CLI or worker thread, not inside an active
event loop. It does not watch Obsidian edits or select an embedding provider.

Cache writes are not transactional with canonical publication. A storage failure
after upsert may leave the newly approved target in the derived index while the
operation reports failure. Keep the canonical approval and other indexed notes,
inspect the target, and retry publication after repairing the cache. Do not undo
the approved note or infer revision freshness from an ID-membership audit; the
source-revision and final canonical checks belong to the consistency extension
in PR #29.
The reviewed lifecycle in PR #30 is required to create superseded or retired records. After a reviewed correction or retirement moves the old SQLite record into a
terminal inactive state, `remove_inactive(old_id)` removes its ID from every
derived vector space and verifies absence from each stored space. The synchronous
adapter exposes the same operation for
offline publication. It refuses a still-approved ID. If cleanup fails, the
canonical review state remains durable; retry cleanup and run `audit_ids`.


The builder rechecks the canonical note revision and current SQLite review state
after resolving each approved snapshot. A review retirement or vault edit inside
the final refresh/audit resolution fails the operation instead of certifying an
earlier snapshot. The canonical edit/review is preserved; stale derived entries
can be refreshed or explicitly removed and retried. This does not make external
edits after the final check atomic; retain the single-worker editing boundary.

## Landed builder contract and consistency extension

Each build, refresh, or ID audit pins the provider's declared model, version,
and dimension for that operation. A declaration change across an async boundary,
including a different model/version with the same dimension, fails with
`IndexBuildError`; outputs are never relabelled into another space. Restore the
intended provider contract before retrying. A rebuild still uses a private new
index, and never switches or overwrites a caller's active index. An embedding
failure before upsert leaves an existing refresh target intact. A storage error
after a write may leave a partial derived candidate; inspect it or rebuild in a
new directory rather than treating the failed operation as a successful switch.

The landed base in PR #21 supplies fresh builds, explicit single-note refresh,
and ID-membership checks. It does not automatically attach semantic retrieval to chat, choose a
deployment encoder, or establish semantic quality. Stored source-revision audits,
post-write canonical race checks, and inactive cleanup are the follow-up
consistency scope implemented by this PR #29. In PR #21 alone, an ID audit cannot
certify vector freshness, and human changes after the final pre-write snapshot
can make a candidate stale. Readers must continue resolving IDs against the
current approved SQLite/vault state; deployment and reader switching require
separate review.
