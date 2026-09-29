# Phase 2 — Memory implementation plan

Baseline: `main` at `769d3c7`, with Phase 1 implementation complete and live OpenAI/browser checks [pending](phase1-verification.md). Phase 2 work uses separate feature branches and PRs. No Phase 2 branch is merged to `main` without user approval.

Conversation history and long-term memory are separate. SQLite keeps full successful conversation turns and memory metadata; an editable Obsidian vault holds selected long-term memory; a vector index is only a search acceleration layer. Every candidate must retain provenance, importance, confidence, timestamps, and an explicit inference marker. A new candidate that conflicts with an explicit user statement waits for review instead of replacing it.

| Task | Small deliverable | Depends on | Can run with |
| --- | --- | --- | --- |
| P2-01 | Shared memory record and six categories, provenance and score validation | Phase 1 main | P2-02, P2-03, P2-04 |
| P2-02 | SQLite migration and conversation transcript persistence across restarts, with bounded prompt context | Phase 1 main | P2-01, P2-03, P2-04 |
| P2-03 | Obsidian Markdown vault adapter with safe paths, editable files and revision checks | Phase 1 main | P2-01, P2-02, P2-04 |
| P2-04 | Chroma/Qdrant comparison and vendor-neutral vector search contract | Phase 1 main | P2-01, P2-02, P2-03 |
| P2-05 | SQLite repository for memory metadata and candidate lifecycle | P2-01, P2-02 | P2-03, P2-04 after integration |
| P2-06 | Memory writing and Obsidian/SQLite consistency rules | P2-01, P2-03, P2-05 | P2-07 contract work |
| P2-07 | Retrieval with provenance, importance, confidence, freshness and conflict handling | P2-04, P2-05, P2-06 | P2-08 design |
| P2-08 | Stage typed Self Memory successes, failures and corrections with provenance and a reusable lesson; approval remains explicit | P2-05, P2-06 | P2-07 implementation |
| P2-09 | Consolidation pipeline: extract candidates, deduplicate, flag conflicts, persist approved updates, refresh index | P2-06, P2-07, P2-08 | tests/docs outside shared code |
| P2-10 | Cross-component tests, migration tests, failure-path tests and operational docs | Each corresponding task | Independent test files where possible |

P2-01/02, P2-03, and P2-04 are the first parallel tracks. They use separate worktrees and avoid shared schema/files. P2-05 onward waits for their contracts to be integrated; schema migrations are serialized. P2-04 does not choose or implement a concrete vector engine until the comparison is recorded. Embeddings and live provider checks can wait for a key; all current tasks can be tested without one.

The first implementation slice was P2-01 through P2-04. P2-08 adds a recording API; automatic extraction and consolidation remain planned. Each PR must keep the app runnable and carry focused tests.

## P2-08 Self Memory recording

Self Memory describes observed assistant behavior, not a fact about the user. Each event has one of three kinds: `success` (what worked and what to repeat), `failure` (what failed and what to change next time), or `correction` (what was wrong and what to use instead). The observation and guidance are both required. Callers supply a traceable `source`, `origin`, importance and confidence; the `self-kind:*` tag preserves the kind within the existing memory record and vault format. The category is always `self`.

`SelfMemoryRecorder.record` submits a new candidate through `MemoryWriter.submit`. It does not approve, edit, or delete an earlier memory, including one contradicted by a correction. A reviewer may inspect the candidate and call `MemoryWriter.approve` to publish it to Obsidian. Candidate extraction from conversations, conflict resolution and retirement of outdated approved lessons belong to P2-09; a correction must not silently override a previous approved note.
