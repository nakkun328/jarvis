# Memory design

## Categories

User memory, project memory, conversation memory, work state, temporary memory, and self memory are separate concepts. A conversation transcript alone is not long-term memory.

## Planned storage

- SQLite: conversations, tasks, tool executions, device state, metadata, and temporary state.
- Obsidian Markdown vault: long-term material the user can inspect and edit.
- Vector index: semantic retrieval. It is not implemented in Phase 0.

Before implementing the vector index in Phase 2, compare Chroma and Qdrant against local setup, data portability, indexing needs, operational cost, and cross-device deployment. Record the decision and migration path here.

Candidate metadata: ID, type, source, creation and update times, importance, confidence, tags, project, and last access. AI inferences must be marked as such. Explicit user statements take precedence; conflicting memories need review instead of blind overwrite.

Consolidation will extract useful facts from logs, search for existing memories, detect duplicates and conflicts, update the vault and metadata, then refresh the vector index. Retrieved facts will retain source and freshness information.
