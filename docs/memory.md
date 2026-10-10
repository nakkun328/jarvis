# Memory design

## Categories

User memory, project memory, conversation memory, work state, temporary memory, and self memory are separate concepts. A conversation transcript alone is not long-term memory.

## Storage boundaries

- SQLite: conversations, tasks, tool executions, device state, metadata, and temporary state.
- Obsidian Markdown vault: long-term material the user can inspect and edit.
- Vector index: derived semantic retrieval cache, rebuildable from reviewed notes.

The [vector decision](vector-search.md) compares Chroma and Qdrant and records the initial local Chroma choice and migration path.

Candidate metadata: ID, type, source, creation and update times, importance, confidence, tags, project, and last access. AI inferences must be marked as such. Explicit user statements take precedence; conflicting memories need review instead of blind overwrite.

The [Phase 2 task plan](phase2-plan.md) defines dependency and parallel work. The first slice defines a storage-neutral record and persists successful conversation turns in SQLite. This transcript is not treated as an approved long-term memory fact.

The next SQLite migration adds `memory_records` for candidates. `MemoryRepository.add` always creates a `pending` candidate and refuses to replace an existing ID. Review may move it to `conflict` or `rejected`; approval requires a vault note revision and uses a compare-and-swap state update. Rejected records are terminal. Approved records may later be superseded or retired through explicit review; their content remains in SQLite and Obsidian for history. This layer does not automatically extract facts from conversations or write Obsidian notes. `MemoryWriter` coordinates the vault write before recording approval. Candidate content is retained in SQLite for review; an approved vault note is the user-editable long-term copy.

`MemoryWriter.submit` stages a candidate without approving it. After explicit review, `MemoryWriter.approve` creates its UUID note with provenance, category, importance, confidence, tags, and project metadata; reads the note back; then records its revision and approved status in SQLite. It never replaces an existing note. If SQLite fails after note creation, the candidate remains pending and a retry may use the note only when its body and metadata still match exactly. A changed or malformed note needs review. If a concurrent review rejects the candidate after note creation, an unmatched note may remain; do not delete it automatically. An approved note missing from the vault is reported as a consistency error and is not silently recreated. After approval, user edits to the note are retained. Retrieval must read the current vault note rather than treating the SQLite candidate snapshot as the latest content.

SQLite schema v4 adds `memory_review_events`. Each successful `MemoryRepository.transition` writes one event in the same transaction as the compare-and-swap status update. Events record the memory ID, previous and new status, action (`flag_conflict`, `approve`, or `reject`), actor, UTC time, and the vault revision used for approval. `MemoryRepository.review_events(id)` returns them in insertion order. A failed update or audit insert leaves both state and event history unchanged. `MemoryWriter.approve` and `MemoryConsolidator.publish_reviewed` accept an optional caller-supplied `actor`; callers that do not supply one are recorded as `unknown`, not attributed to a human. Automatic conflict flags use `system:consolidator`. Actor strings are attribution supplied by the caller, not authenticated identity. Existing v3 records are preserved when migrating and have no invented retrospective events. This audit covers repository state transitions; it does not audit direct SQLite writes or human edits to vault notes.

SQLite schema v5 preserves v4 rows and review events while adding `supersedes_id`, `supersedes_revision`, `replaced_by_id`, `superseded`, and `retired`. `MemoryWriter.submit_correction(old_id, record)` validates the current old note and stages a new candidate linked to its observed revision. The old record remains approved until a reviewer approves the new one. One SQLite transaction then approves the new record, supersedes the old, records `replaced_by_id`, and writes both the approval and lifecycle audit events. Competing replacements and retirement make this compare-and-swap fail. A new note left after a failed SQLite transaction is reused on retry only if it still matches the candidate. The old note stays on disk for inspection but its non-approved state removes it from retrieval. `MemoryWriter.retire(id, actor, reason)` similarly records a reviewed retirement and audit event without deleting the note. `MemoryRepository.lifecycle_events(id)` returns supersession and retirement events. Actor labels are caller-supplied, not authenticated identities.

`SelfMemoryRecorder.record` stages a `self` candidate with a `self-kind:success`, `self-kind:failure`, or `self-kind:correction` tag. It records an observed outcome and concrete future guidance, with caller-supplied provenance and scores. It uses the same review and publication path as other memories. A correction can supply `supersedes_id` to link an earlier approved Self Memory; without that ID it remains an independent candidate.

`MemoryRetriever.search_text` scans current approved vault notes for a small personal corpus; `search_vector` accepts candidates from the `VectorIndex` contract and resolves each ID through the same SQLite and vault checks. A local Chroma adapter implements that contract. The versioned embedding contract, rebuild/refresh operations, and ID/revision audits are described in [index operations](index-rebuild.md). The optional remote embedding adapter is a separate change in PR #25. Vector retrieval widens its candidate window when unapproved or unavailable entries occupy the top results. Results include the current body, source, origin, importance, confidence, note revision, and an `edited_since_approval` flag. Vault changes to scores, tags, or project are validated and reflected in results. Category, source, origin, and creation time are immutable provenance; edits to those fields need review and suppress the note from retrieval. Missing or invalid approved notes are reported as issues and never replaced with the old SQLite text. Pending, rejected, superseded, and retired candidates are excluded. Matching `conflict` candidates appear separately as unresolved material, never as approved facts; semantic conflict detection remains a later consolidation task.

The `stale` flag uses the SQLite record's last reviewed `updated_at` and a configurable window (180 days by default). It does not establish that the underlying real-world claim is current. A human edit changes the note revision but does not reset that review clock. Text ranking uses lexical overlap, with explicit user statements ahead of inferences at equal relevance. Vector scores are passed through as index scores and are never interpreted as confidence or compared across embedding spaces.

`MemoryConsolidator` stages explicit conversation statements and typed Self Memory events for review. It detects exact duplicates and topic conflicts, then publishes only after a reviewer approves a candidate. An optional index refresher updates the derived vector cache. With the reviewed lifecycle now on main, corrections and retirements can remove old IDs through this pending index adapter. It does not silently infer or overwrite facts. See [consolidation](consolidation.md). A deterministic general candidate extractor for user/project/self statements (Japanese and English, not yet wired into any entrypoint) is described in [candidate detection](candidate-detection.md). The [local review CLI](memory-review.md) is on main; its direct review operations require explicit derived-index refresh or cleanup afterwards.

## Owner-approved exception: automatic memory from chat

> **A second deliberate exception**, recorded by the owner on 2026-10-10 ("調べたり喋ってたら勝手に記憶が出来上がっていく"). It reverses, for chat, the earlier stance that conversation text is never turned into memory without human review. Both switches default **off**. Full description, filters and privacy notes: [chat-auto-memory.md](chat-auto-memory.md).

With `JARVIS_CHAT_MEMORY_AUTO=1`, after a chat turn has been answered, the default chat provider extracts up to three short facts about the owner from the owner's own message; each is verified deterministically (verbatim quote, sensitive-data and instruction filters, dedupe, caps) and staged as a `pending` candidate of origin `chat` (tag `chat-auto`). With `JARVIS_CHAT_MEMORY_AUTO_APPROVE=1` (requires the vault) they are approved at once through `MemoryWriter.approve` with the audit actor `auto:chat`; the `/memory` screen labels them 「自動承認(会話)」 and the model-facing context prefixes them `(会話由来・自動承認)`. The human-only withdrawal below also works for them. The `chat-auto` candidates and the `auto:chat` approvals are the only chat-derived memory; `GeneralCandidateExtractor` and `ExplicitExtractor` stay unwired as before.

## Owner-approved exception: research auto-approval

> **This is a deliberate exception to the contract above ("only reviewed, currently approved notes
> reach a model").** Decision recorded by the owner on 2026-10-10. Both switches default **off**.

**What.** Memory candidates whose origin is `research` (staged from a completed research session,
see [research-to-memory.md](research-to-memory.md)) can be approved by the system instead of by a
person, one memory per verified claim. Candidates from candidate detection, consolidation or
anything else are not touched and keep the human review described above (chat candidates have
their own, separate switch, see above).

**Why.** The owner prefers that researched facts become usable memory without a review step.

**Switches** (both `false` by default; invalid values refuse to start):

| Variable | Effect | Requires |
| --- | --- | --- |
| `JARVIS_RESEARCH_MEMORY_AUTO_APPROVE` | when research candidates are staged, safe ones are approved immediately | `JARVIS_MEMORY_VAULT_PATH` (approval publishes a vault note; without a vault there is no approval at all, so the setting is refused) |
| `JARVIS_RESEARCH_MEMORY_AUTO_STAGE` | when a research session completes with at least one verified claim, its claims are staged automatically | `JARVIS_RESEARCH_ENABLED` |

Combinations: neither = staging only by the 「記憶の候補にする」 button, human review (unchanged);
`AUTO_APPROVE` only = the button stages and approves; `AUTO_STAGE` only = completion stages
pending candidates, a person reviews; both = completion stages and approves with no person involved.

**How.** Approval goes through the existing `MemoryWriter.approve` (one vault note, the existing
compare-and-swap transition); there is no second write path. It is recorded in the review audit
with the fixed actor `auto:research`, which the API reports as `automatic` (the actor text itself is
not exposed). Re-staging a session never approves twice.

**Mitigations** (research text is untrusted web text; a verified quote only proves the quote is on
the page). A claim stays `pending` for human review, and is *not* auto-approved, when:

- its source is weak: type is `unknown`, `blog`, `community` or `forum` **and** the authority rating
  is missing or below 0.5 (constants in `backend/research/claim_safety.py`);
- the claim, quote or source title reads like an instruction aimed at a model (deterministic
  patterns in the same module, best effort, Japanese and English, after NFKC folding);
- it is in an open conflict, or beyond the 5-per-session cap (unchanged from staging);
- the writer fails for any reason (it stays pending).

In the model-facing context a research note's text starts with a fixed label:
`(調査由来・自動承認)` for an automatic approval, `(調査由来)` for a human-approved research note.
The `/memory` screen shows the label 「自動承認(調査)」 with the source URL and retrieval date.

**Withdrawing.** On the `/memory` detail page of an auto-approved note, 「この自動承認を撤回」 calls
`POST /api/memory/notes/{id}/withdraw` (same-origin and `X-Jarvis-Confirm: 1`, login when on; no
model or agent path). It uses the existing retirement transition: the record becomes `retired`
(never retrieved again), with a lifecycle event by actor `web:owner`. Nothing is deleted: the vault
note stays on disk (as for every retirement) and the approval history stays. Only auto-approved
research or chat notes (approved by their own origin's automatic actor) can be withdrawn this way;
anything else is refused. If the vault note was edited or
is unreadable the withdrawal still succeeds against the revision recorded at approval. A derived
vector index is not refreshed by this action; retrieval re-checks SQLite status, so a retired note
is never returned.

`python -m backend.doctor` has an area `research_memory` that states plainly when automatic
approval is on (flags only, no content).

## Memory activity feed

So that the owner can see WHEN a memory was made, chat auto-memory (`ChatAutoMemory`), research staging and auto-approval (`ResearchMemoryCandidates`, including the manual button), the 「忘れて」 command and the human withdraw route publish small events to an in-process feed (`backend/memory/events.py`). The publisher is injected (`publisher=` arguments); `create_app` wires one `MemoryActivityFeed` per app and tests use their own, so there is no global singleton.

- **Bounded and volatile.** A ring buffer of the last 100 events. Nothing is stored in SQLite and there is no migration (schema v10); a restart empties it and `seq` starts again at 1 (the page notices `latest` going backwards and follows).
- **Event.** `{seq, at, kind, origin, memory_id, summary}`: `seq` rises by one per event and never repeats; `at` is UTC ISO; `kind` is `staged` (a candidate waits for review), `approved` (approved automatically, `auto:chat` / `auto:research`) or `withdrawn` (withdrawn by the owner or by 「忘れて」); `origin` is `chat` or `research`; `summary` is the first line of the note, control/format/bidi characters removed, at most 80 characters. Only what a call actually changed is announced: a repeated staging of the same claims publishes nothing, and a candidate approved on the spot is one `approved` event, not two.
- **Never blocks, never leaks.** `safe_publish` swallows any publisher failure and logs only the fixed code `memory_event_publish_failed`. The summary is the owner's own content and is never logged.
- **Endpoint.** `GET /api/memory/activity?after=<seq>` (behind the login layer; `Cache-Control: no-store`; read-only) returns `{"latest": <int>, "events": [...], "configured": <bool>, "chat_enabled": <bool>, "research_enabled": <bool>}`. `events` holds the events with `seq > after`, oldest first, at most 50. `after` defaults to 0 and must be a non-negative integer of at most 15 digits, otherwise `422 {"detail": "invalid_after"}`. `configured` is whether a memory vault is configured (the Activity View dims its MEMORY node when it is not); `chat_enabled` / `research_enabled` say whether chat auto-memory / research auto-staging are switched on.
- **Page.** The chat page and the Research screen poll it for a short window after a reply or a finished research (see [chat.md](chat.md), "Activity events").
