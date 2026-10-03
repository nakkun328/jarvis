# Phase 2 — Memory implementation plan

Initial planning baseline (historical): `main` at `769d3c7`, with Phase 1 implementation complete and live OpenAI/browser checks [pending](phase1-verification.md). Phase 2 work uses separate feature branches and PRs. No Phase 2 branch is merged to `main` without user approval.

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


## Current requirement mapping — 2026-10-03 after #33

Checked main: `ce0118c9bb8958e055ecae46823f82a4fda2cb61`. Earlier baseline
and scheduling paragraphs are original planning history. Current status follows
Git/GitHub, and this table is not permission to merge any pending PR.

| Initial task | Available on main | Pending scope / limits |
| --- | --- | --- |
| P2-01 | Six memory categories, provenance, scores and origin validation | No model-quality claim |
| P2-02 | Full successful transcripts, bounded prompts, SQLite migrations | Process-local locks; one worker |
| P2-03 | Safe editable Obsidian notes and revision checks | Writers/editors must quiesce for snapshot |
| P2-04 | Chroma/vector contract and canonical resolution of vector IDs | Versioned embeddings #18; semantic-query API #26; not wired to chat |
| P2-05 | Candidates, explicit review, correction/supersession/retirement audit | Physical derived cache removal #29 |
| P2-06 | Reviewed publication, rollback and matching-note retry | Derived refresh/rebuild #21/#24; no automatic index switch |
| P2-07 | Current approved lexical/vector retrieval and bounded opt-in lexical chat | Semantic retrieval #26 remains separately called; no semantic chat |
| P2-08 | Typed successes/failures/corrections with provenance and explicit approval | No generic automated reflection/extraction |
| P2-09 | Labelled Remember/Self-event candidates, dedup/conflict/review/lifecycle | General extraction excluded; concrete derived refresh/cleanup #24/#29 |
| P2-10 | Migration, vault, canonical lifecycle, chat, shared-resource cleanup and Linux CI gate | Recovery #34 and this runbook remain Draft |

Shared-resource cleanup #35 has landed. Gemini #31 now needs only its own
provider/configuration/tests/docs scope against the updated main. Gate #33 has
landed with actual Linux/Python 3.11/Node 22 execution receipts. Recovery #34 is
the next review candidate, checked with current main; runbook #32 follows the
available code. Gemini #31 is a separate provider candidate. No pending Draft
is authorized for merge.
The independent embedding track starts #18, then #21→#24→#26→#29; optional
OpenAI embedding #25 follows #18. No pending Draft is authorized for landing.

OpenAI live validation remains pending. No live API was used in this cycle;
future necessary live checks use Gemini/gemini-2.5-flash. Browser UI remains
unverified while permissions are unavailable. Fake providers/embeddings prove
routing, integrity and cleanup rather than model semantic quality. Authentication,
multiworker, scheduler and Phase3 remain outside the current scope.
