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

`SelfMemoryRecorder.record` stages a `self` candidate with a `self-kind:success`, `self-kind:failure`, or `self-kind:correction` tag. It records an observed outcome and concrete future guidance, with caller-supplied provenance and scores. It uses the same review and publication path as other memories. A correction is a new pending candidate; it never overwrites an approved lesson. Review of conflicting lessons and retirement of stale ones remain future consolidation work.

`MemoryRetriever.search_text` scans current approved vault notes for a small personal corpus; `search_vector` accepts candidates from the `VectorIndex` contract and resolves each ID through the same SQLite and vault checks. A local Chroma adapter implements that contract; embedding generation and canonical index maintenance are still pending. Vector retrieval widens its candidate window when unapproved or unavailable entries occupy the top results. Results include the current body, source, origin, importance, confidence, note revision, and an `edited_since_approval` flag. Vault changes to scores, tags, or project are validated and reflected in results. Category, source, origin, and creation time are immutable provenance; edits to those fields need review and suppress the note from retrieval. Missing or invalid approved notes are reported as issues and never replaced with the old SQLite text. Pending and rejected candidates are excluded. Matching `conflict` candidates appear separately as unresolved material, never as approved facts; semantic conflict detection remains a later consolidation task.

The `stale` flag uses the SQLite record's last reviewed `updated_at` and a configurable window (180 days by default). It does not establish that the underlying real-world claim is current. A human edit changes the note revision but does not reset that review clock. Text ranking uses lexical overlap, with explicit user statements ahead of inferences at equal relevance. Vector scores are passed through as index scores and are never interpreted as confidence or compared across embedding spaces.

Consolidation will extract useful facts from logs, search for existing memories, detect duplicates and conflicts, update the vault and metadata, then refresh the vector index. Retrieved facts will retain source and freshness information.
