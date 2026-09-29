# Memory consolidation (P2-09 draft)

`MemoryConsolidator` is an offline, key-free proposal path. It reads completed SQLite conversation turns or typed `SelfEvent` values, extracts candidate records, checks existing candidates and current approved Obsidian notes, and stages new records in SQLite. It never runs during chat, approves a candidate, edits an approved note, or deletes old history automatically.

## Extraction and provenance

The built-in `ExplicitExtractor` accepts only a user's completed turn containing exactly `Remember <topic>: <statement>` (or `Remember that <topic>: <statement>`). `<topic>` is an ASCII label of at most 64 characters, such as `reply_style`. Ordinary chat, assistant replies, and a final user message without a persisted assistant reply are ignored. A staged record keeps `source=conversation:<conversation UUID>:message:<SQLite message ID>` and `origin=user_explicit`. This source identifies the precise transcript row. The text after the colon is retained as the proposed content. The default importance is 0.6 and confidence is 1.0 for an explicit statement; these are candidate metadata, not proof that the statement is still current.

A `SelfEvent` carries an event ID, outcome kind (`success`, `failure`, or `correction`), topic, observation, future guidance, original source, origin, scores, and optional project. It creates a `self` candidate with a `self-kind:*` tag and the same two-line content format as `SelfMemoryRecorder`. A correction can include `supersedes_id` to link an earlier approved Self Memory; it remains a separate review item until approval. `CandidateExtractor` may be replaced with a local extractor that returns `ExtractedCandidate` values. The pipeline validates the returned records before writing any candidate, and callers remain responsible for trustworthy source identifiers and scores. No API key or model is needed.

## Review decisions

The pipeline reads the **current vault note** for every approved memory and validates its immutable provenance through `MemoryRetriever.get_approved`. A missing or invalid approved note stops the batch before staging anything. It compares within the same category and project:

- Exact duplicates use Unicode NFKC, case folding, and whitespace folding. Within the same category and project, normalized content and origin must match; this also catches older approved notes with no topic tag. A repeated assertion is skipped, retaining the first stored source. Its deterministic UUID prevents a second identical candidate under the same topic from being inserted by concurrent runs. The `StageResult.duplicates` list exposes skipped assertions to the caller.
- Different content under the same `consolidation-topic:*` tag is a possible conflict. New candidates are marked `conflict`; when two new candidates disagree, both enter that state. An older approved note stays untouched. An untagged legacy approved note in the same category and project conservatively forces review because there is no reliable topic to compare.
- New candidates without a known disagreement remain `pending`. Pending and conflict candidates are excluded from approved retrieval. Rejected records remain terminal and suppress an identical repeat.

Superseded and retired records remain available for history but do not count as
current duplicates or conflicts. A rejected deterministic candidate still cannot
be silently restaged. If new evidence proposes the same content as an inactive
record, the pipeline gives that proposal a new deterministic ID tied to its
source, so it can receive another review without reactivating old history.

These are **review cues**, not semantic truth judgments. Paraphrases with different wording can evade exact duplicate detection; unrelated assertions under one broad topic can be marked as conflicting. Choose narrow topics and review candidate content alongside its source transcript or event. An extractor must not treat assistant text or speculative content as `user_explicit`.

## Publication and index refresh

A reviewer inspects a pending or conflict record and its source, then explicitly calls `MemoryConsolidator.publish_reviewed(memory_id)` (or `MemoryWriter.approve`). This writes a new Obsidian note through the writer and moves its SQLite status to `approved`. For a linked correction, the same SQLite transaction marks the old record `superseded` and records the relation and audit. The old note remains on disk. If an `IndexRefresher` adapter was provided, `publish_reviewed` refreshes the canonical new memory and removes the inactive old ID. The adapter owns embedding generation and the configured vector space. If refresh or cleanup fails, `IndexRefreshError` is raised; the SQLite approval remains durable, and retrying `publish_reviewed` retries the derived index work without overwriting either note. `retire_reviewed(memory_id, actor, reason)` similarly retires a fact and removes its index ID; calling it again after cleanup failure retries removal. Without an index adapter, text retrieval still works and an index audit/rebuild is needed to remove stale entries.

Topic labels are review cues only; they do not prove semantic conflict. Correction and retirement remain explicit human decisions. Actor labels are caller-supplied attribution, not authenticated identities.

## Minimal offline use

```python
from uuid import UUID

from backend.core.database import Database
from backend.memory.consolidation import MemoryConsolidator
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.retrieval import MemoryRetriever
from backend.memory.writer import MemoryWriter

# Initialize once; paths should point to the user's configured database and vault.
database = Database(db_path)
database.initialize()
repository = MemoryRepository(database)
vault = ObsidianVault(vault_path)
writer = MemoryWriter(repository, vault)
retriever = MemoryRetriever(repository, vault)
consolidator = MemoryConsolidator(database, writer, retriever)
result = consolidator.stage_conversation(UUID(conversation_id))
# Inspect result.pending and result.conflicts with their source records.
# Following explicit review only: consolidator.publish_reviewed(selected_id)
```
