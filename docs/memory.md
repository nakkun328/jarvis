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

The next SQLite migration adds `memory_records` for candidates. `MemoryRepository.add` always creates a `pending` candidate and refuses to replace an existing ID. Review may move it to `conflict` or `rejected`; approval requires a vault note revision and uses a compare-and-swap state update. Approved and rejected states are terminal in this repository. This layer does not automatically extract facts from conversations or write Obsidian notes. That coordination belongs to the later memory writer, which must persist the note before recording approval. Candidate content is retained in SQLite for review; an approved vault note will be the user-editable long-term copy.

Consolidation will extract useful facts from logs, search for existing memories, detect duplicates and conflicts, update the vault and metadata, then refresh the vector index. Retrieved facts will retain source and freshness information.
