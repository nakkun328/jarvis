# Memory consolidation (P2-09 draft)

`MemoryConsolidator` is an offline, key-free proposal path. It reads completed SQLite conversation turns or typed `SelfEvent` values, extracts candidate records, checks existing candidates and current approved Obsidian notes, and stages new records in SQLite. It never runs during chat, approves a candidate, edits an approved note, or deletes old history automatically.

## Extraction and provenance

The built-in `ExplicitExtractor` accepts only a user's completed turn containing exactly `Remember <topic>: <statement>` (or `Remember that <topic>: <statement>`). `<topic>` is an ASCII label of at most 64 characters, such as `reply_style`. Ordinary chat, assistant replies, and a final user message without a persisted assistant reply are ignored. A staged record keeps `source=conversation:<conversation UUID>:message:<SQLite message ID>` and `origin=user_explicit`. This source identifies the precise transcript row. The text after the colon is retained as the proposed content. The default importance is 0.6 and confidence is 1.0 for an explicit statement; these are candidate metadata, not proof that the statement is still current.

A `SelfEvent` carries an event ID, outcome kind (`success`, `failure`, or `correction`), topic, observation, future guidance, original source, origin, scores, and optional project. It creates a `self` candidate with a `self-kind:*` tag and the same two-line content format as `SelfMemoryRecorder`. A correction remains a new review item; it does not retire an approved lesson. `CandidateExtractor` may be replaced with a local extractor that returns `ExtractedCandidate` values. The pipeline validates the returned records before writing any candidate, and callers remain responsible for trustworthy source identifiers and scores. No API key or model is needed.

## Review decisions

The pipeline reads the **current vault note** for every approved memory and validates its immutable provenance through `MemoryRetriever.get_approved`. A missing or invalid approved note stops the batch before staging anything. It compares within the same category and project:

- Exact duplicates use Unicode NFKC, case folding, and whitespace folding. Within the same category and project, normalized content and origin must match; this also catches older approved notes with no topic tag. A repeated assertion is skipped, retaining the first stored source. Its deterministic UUID prevents a second identical candidate under the same topic from being inserted by concurrent runs. The `StageResult.duplicates` list exposes skipped assertions to the caller.
- Different content under the same `consolidation-topic:*` tag is a possible conflict. New candidates are marked `conflict`; when two new candidates disagree, both enter that state. An older approved note stays untouched. An untagged legacy approved note in the same category and project conservatively forces review because there is no reliable topic to compare.
- New candidates without a known disagreement remain `pending`. Pending and conflict candidates are excluded from approved retrieval. Rejected records remain terminal and suppress an identical repeat.

These are **review cues**, not semantic truth judgments. Paraphrases with different wording can evade exact duplicate detection; unrelated assertions under one broad topic can be marked as conflicting. Choose narrow topics and review candidate content alongside its source transcript or event. An extractor must not treat assistant text or speculative content as `user_explicit`.

## Publication and index refresh

A reviewer inspects a pending or conflict record and its source, then explicitly calls `MemoryConsolidator.publish_reviewed(memory_id)` (or the existing `MemoryWriter.approve`). This writes a new Obsidian note through the writer and moves its SQLite status to `approved`. It does **not** update an existing approved note. If an `IndexRefresher` adapter was provided, `publish_reviewed` passes the canonical, vault-resolved record to it after approval. The adapter owns embedding generation and the configured vector space. If refresh fails, the method raises `IndexRefreshError`; the note and approval remain durable, and calling `publish_reviewed` again retries refresh without recreating or overwriting the note. Without an index adapter, text retrieval still works and no vector refresh is claimed.

The current repository has terminal approved/rejected states and no supersession relation, reviewer identity audit, or semantic conflict resolver. Consequently, this draft does not automatically apply approved updates to an earlier note, retire superseded facts, or guarantee a synchronized vector index when no adapter is supplied. These require a reviewed lifecycle design before P2-09 can be called complete.

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
