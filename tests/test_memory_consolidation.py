"""Consolidation keeps source evidence, review state, and vault truth separate."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.core.database import Database
from backend.memory.consolidation import (
    ConsolidationError,
    ConversationEvidence,
    ExplicitExtractor,
    ExtractedCandidate,
    IndexRefreshError,
    MemoryConsolidator,
    SelfEvent,
)
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.self_memory import SelfMemoryKind
from backend.memory.writer import MemoryWriter


def _system(tmp_path: Path, *, refresher: object = None):
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    retriever = MemoryRetriever(repository, vault)
    pipeline = MemoryConsolidator(database, writer, retriever, index_refresher=refresher)
    return pipeline, database, repository, writer, vault, retriever


def _candidate(content: str, *, source: str = "test:1") -> ExtractedCandidate:
    return ExtractedCandidate(
        category=MemoryCategory.USER,
        topic="reply_style",
        content=content,
        source=source,
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
    )


class FixedExtractor:
    def __init__(self, *candidates: ExtractedCandidate) -> None:
        self.candidates = candidates

    def extract(self, evidence: ConversationEvidence):
        return self.candidates


def _stage(pipeline: MemoryConsolidator, *candidates: ExtractedCandidate):
    pipeline.extractor = FixedExtractor(*candidates)
    return pipeline.stage([ConversationEvidence(uuid4(), 1, "user", "unused")])


def test_persisted_conversation_only_extracts_explicit_completed_user_turns(
    tmp_path: Path,
) -> None:
    pipeline, database, repository, _, vault, _ = _system(tmp_path)
    conversation_id = uuid4()
    with database.connect() as connection, connection:
        connection.execute(
            "INSERT INTO conversations (id, created_at, updated_at) VALUES (?, '', '')",
            (str(conversation_id),),
        )
        for role, content in (
            ("user", "Remember reply_style: Prefer concise answers."),
            ("assistant", "Understood."),
            ("user", "My telescope is blue."),
            ("assistant", "Thanks."),
            ("user", "Remember address: I live at a secret address"),
        ):
            connection.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content, created_at) "
                "VALUES (?, ?, ?, '')",
                (str(conversation_id), role, content),
            )
    result = pipeline.stage_conversation(conversation_id)
    assert len(result.pending) == 1
    record = result.pending[0].record
    assert record.content == "Prefer concise answers."
    assert record.source == f"conversation:{conversation_id}:message:1"
    assert record.origin is MemoryOrigin.USER_EXPLICIT
    assert record.tags == ("consolidation-topic:reply_style",)
    assert repository.get(record.id).status is MemoryStatus.PENDING
    assert not vault.root.exists()
    repeated = pipeline.stage_conversation(conversation_id)
    assert repeated.pending == ()
    assert len(repeated.duplicates) == 1


def test_duplicates_are_normalized_and_first_source_is_retained(tmp_path: Path) -> None:
    pipeline, _, repository, _, _, _ = _system(tmp_path)
    first = _stage(pipeline, _candidate("Prefer  short replies", source="test:a"))
    second = _stage(pipeline, _candidate("prefer short replies", source="test:b"))
    assert len(first.pending) == 1
    assert second.pending == ()
    assert len(second.duplicates) == 1
    assert repository.get(first.pending[0].record.id).record.source == "test:a"
    assert len(repository.list_by_status(MemoryStatus.PENDING)) == 1


def test_same_topic_disagreement_is_conflict_and_never_replaces_approved_note(
    tmp_path: Path,
) -> None:
    pipeline, _, repository, _, vault, retriever = _system(tmp_path)
    original = _stage(pipeline, _candidate("Prefer concise replies"))
    approved = pipeline.publish_reviewed(original.pending[0].record.id, actor="reviewer:alice")
    assert approved.status is MemoryStatus.APPROVED
    assert [
        (event.action, event.actor) for event in repository.review_events(approved.record.id)
    ] == [("approve", "reviewer:alice")]
    note = vault.read(approved.record.id)
    assert note is not None
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace("concise", "brief"),
        encoding="utf-8",
    )
    result = _stage(pipeline, _candidate("Prefer detailed replies", source="test:2"))
    assert result.pending == ()
    assert len(result.conflicts) == 1
    assert result.conflicts[0].status is MemoryStatus.CONFLICT
    assert [
        (event.action, event.actor)
        for event in repository.review_events(result.conflicts[0].record.id)
    ] == [("flag_conflict", "system:consolidator")]
    assert vault.read(approved.record.id).body == "Prefer brief replies"
    assert retriever.search_text("detailed").matches == ()
    assert [item.record.id for item in retriever.search_text("detailed").conflicts] == [
        result.conflicts[0].record.id
    ]
    assert repository.get(approved.record.id).status is MemoryStatus.APPROVED


def test_duplicate_uses_current_human_edited_note_not_old_sqlite_text(tmp_path: Path) -> None:
    pipeline, _, _, _, vault, _ = _system(tmp_path)
    first = _stage(pipeline, _candidate("Prefer concise replies"))
    memory_id = first.pending[0].record.id
    pipeline.publish_reviewed(memory_id)
    note = vault.read(memory_id)
    assert note is not None
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace("concise", "brief"),
        encoding="utf-8",
    )
    result = _stage(pipeline, _candidate("Prefer brief replies", source="test:2"))
    assert result.pending == ()
    assert result.conflicts == ()
    assert len(result.duplicates) == 1


def test_two_new_disagreeing_candidates_are_both_flagged(tmp_path: Path) -> None:
    pipeline, _, repository, _, _, _ = _system(tmp_path)
    result = _stage(
        pipeline,
        _candidate("Prefer concise replies", source="test:a"),
        _candidate("Prefer detailed replies", source="test:b"),
    )
    assert result.pending == ()
    assert len(result.conflicts) == 2
    assert len(repository.list_by_status(MemoryStatus.CONFLICT)) == 2
    for item in result.conflicts:
        assert [
            (event.action, event.actor) for event in repository.review_events(item.record.id)
        ] == [("flag_conflict", "system:consolidator")]


def test_rejected_candidate_remains_terminal_on_repeated_extraction(tmp_path: Path) -> None:
    pipeline, _, repository, _, _, _ = _system(tmp_path)
    first = _stage(pipeline, _candidate("Prefer concise replies"))
    memory_id = first.pending[0].record.id
    repository.transition(memory_id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED)
    again = _stage(pipeline, _candidate("Prefer concise replies", source="test:second"))
    assert again.pending == ()
    assert again.conflicts == ()
    assert len(again.duplicates) == 1
    assert repository.get(memory_id).status is MemoryStatus.REJECTED


def test_legacy_untagged_approved_record_forces_review(tmp_path: Path) -> None:
    pipeline, _, _, writer, _, _ = _system(tmp_path)
    legacy = MemoryRecord(
        category=MemoryCategory.USER,
        content="Prefers concise replies",
        source="old:conversation",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1,
    )
    writer.submit(legacy)
    writer.approve(legacy.id)
    result = _stage(pipeline, _candidate("Prefers detailed replies"))
    assert len(result.conflicts) == 1


def test_invalid_approved_note_stops_batch_before_any_write(tmp_path: Path) -> None:
    pipeline, _, repository, writer, vault, _ = _system(tmp_path)
    original = MemoryRecord(
        category=MemoryCategory.USER,
        content="Prefers concise replies",
        source="old:conversation",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1,
    )
    writer.submit(original)
    writer.approve(original.id)
    note = vault.read(original.id)
    assert note is not None
    note.path.unlink()
    with pytest.raises(ConsolidationError, match="needs repair"):
        _stage(pipeline, _candidate("Prefers detailed replies"))
    assert repository.list_by_status(MemoryStatus.PENDING) == []


def test_self_event_preserves_kind_guidance_and_provenance(tmp_path: Path) -> None:
    pipeline, _, _, _, _, _ = _system(tmp_path)
    event = SelfEvent(
        event_id="run-42",
        kind=SelfMemoryKind.CORRECTION,
        topic="citation_style",
        observation="Cited the wrong paper",
        guidance="Check the DOI before citing",
        source="tool:search:42",
        origin=MemoryOrigin.TOOL_OBSERVATION,
        importance=0.9,
        confidence=0.95,
        project="research",
    )
    result = pipeline.stage([event])
    assert len(result.pending) == 1
    record = result.pending[0].record
    assert record.category is MemoryCategory.SELF
    assert record.content == (
        "Correction: Cited the wrong paper\nUse instead: Check the DOI before citing"
    )
    assert record.source == "event:run-42:tool:search:42"
    assert record.tags == ("consolidation-topic:citation_style", "self-kind:correction")
    assert record.project == "research"


def test_index_failure_is_reported_after_approval_and_retry_is_safe(tmp_path: Path) -> None:
    class FlakyRefresher:
        calls: list[UUID] = []

        def refresh(self, memory):
            self.calls.append(memory.record.id)
            if len(self.calls) == 1:
                raise RuntimeError("index down")

    refresher = FlakyRefresher()
    pipeline, _, repository, _, vault, _ = _system(tmp_path, refresher=refresher)
    staged = _stage(pipeline, _candidate("Prefer concise replies"))
    memory_id = staged.pending[0].record.id
    with pytest.raises(IndexRefreshError, match="was approved"):
        pipeline.publish_reviewed(memory_id)
    assert repository.get(memory_id).status is MemoryStatus.APPROVED
    before = vault.read(memory_id)
    assert before is not None
    assert pipeline.publish_reviewed(memory_id).status is MemoryStatus.APPROVED
    assert vault.read(memory_id).revision == before.revision
    assert refresher.calls == [memory_id, memory_id]


def test_pluggable_extractor_must_supply_valid_topic_and_record(tmp_path: Path) -> None:
    pipeline, _, repository, _, _, _ = _system(tmp_path)
    bad = replace(_candidate("Some fact"), topic="unsafe/topic")
    with pytest.raises(ValueError, match="topic"):
        _stage(pipeline, bad)
    assert repository.list_by_status(MemoryStatus.PENDING) == []
    assert (
        ExplicitExtractor().extract(ConversationEvidence(uuid4(), 1, "user", "ordinary chat")) == ()
    )
    assert (
        ExplicitExtractor().extract(
            ConversationEvidence(uuid4(), 1, "assistant", "Remember reply_style: terse")
        )
        == ()
    )
