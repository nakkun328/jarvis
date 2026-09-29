# Memory design

## Categories

User memory, project memory, conversation memory, work state, temporary memory, and self memory are separate concepts. A conversation transcript alone is not long-term memory.

## Planned storage

- SQLite: conversations, tasks, tool executions, device state, metadata, and temporary state.
- Obsidian Markdown vault: long-term material the user can inspect and edit.
- Vector index: semantic retrieval. It is not implemented in Phase 0.

Before implementing the vector index in Phase 2, compare Chroma and Qdrant against local setup, data portability, indexing needs, operational cost, and cross-device deployment. Record the decision and migration path here.

Candidate metadata: ID, type, source, creation and update times, importance, confidence, tags, project, and last access. AI inferences must be marked as such. Explicit user statements take precedence; conflicting memories need review instead of blind overwrite.

The [Phase 2 task plan](phase2-plan.md) defines dependency and parallel work. The first slice defines a storage-neutral record and persists successful conversation turns in SQLite. This transcript is not treated as an approved long-term memory fact.

The next SQLite migration adds `memory_records` for candidates. `MemoryRepository.add` always creates a `pending` candidate and refuses to replace an existing ID. Review may move it to `conflict` or `rejected`; approval requires a vault note revision and uses a compare-and-swap state update. Approved and rejected states are terminal in this repository. This layer does not automatically extract facts from conversations or write Obsidian notes. `MemoryWriter` coordinates the vault write before recording approval. Candidate content is retained in SQLite for review; an approved vault note is the user-editable long-term copy.

`MemoryWriter.submit` stages a candidate without approving it. After explicit review, `MemoryWriter.approve` creates its UUID note with provenance, category, importance, confidence, tags, and project metadata; reads the note back; then records its revision and approved status in SQLite. It never replaces an existing note. If SQLite fails after note creation, the candidate remains pending and a retry may use the note only when its body and metadata still match exactly. A changed or malformed note needs review. If a concurrent review rejects the candidate after note creation, an unmatched note may remain; do not delete it automatically. An approved note missing from the vault is reported as a consistency error and is not silently recreated. After approval, user edits to the note are retained. Retrieval must read the current vault note rather than treating the SQLite candidate snapshot as the latest content.

Consolidation will extract useful facts from logs, search for existing memories, detect duplicates and conflicts, update the vault and metadata, then refresh the vector index. Retrieved facts will retain source and freshness information.
