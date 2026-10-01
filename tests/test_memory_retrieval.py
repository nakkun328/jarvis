"""Retrieval uses approved vault notes and preserves review boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.vector import VectorMatch, VectorQuery
from backend.memory.writer import MemoryWriter


def _setup(tmp_path: Path) -> tuple[MemoryWriter, MemoryRepository, ObsidianVault]:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    return MemoryWriter(repository, vault), repository, vault


def _record(content: str, *, origin: MemoryOrigin = MemoryOrigin.USER_EXPLICIT) -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source="conversation:123",
        origin=origin,
        importance=0.6,
        confidence=0.8,
        tags=("preferences",),
    )


def test_text_search_uses_human_edits_and_keeps_provenance(tmp_path: Path) -> None:
    writer, repository, vault = _setup(tmp_path)
    record = _record("Prefers concise replies")
    writer.submit(record)
    writer.approve(record.id)
    note = vault.read(record.id)
    assert note is not None
    note.path.write_text(
        note.path.read_text(encoding="utf-8")
        .replace("concise", "detailed")
        .replace("importance: 0.6", "importance: 0.9"),
        encoding="utf-8",
    )

    result = MemoryRetriever(repository, vault).search_text("detailed")
    assert len(result.matches) == 1
    found = result.matches[0]
    assert found.record.id == record.id
    assert found.record.content == "Prefers detailed replies"
    assert found.record.source == "conversation:123"
    assert found.record.origin is MemoryOrigin.USER_EXPLICIT
    assert found.record.confidence == 0.8
    assert found.record.importance == 0.9
    assert found.edited_since_approval
    assert not found.stale
    assert result.conflicts == ()
    assert result.issues == ()
    assert MemoryRetriever(repository, vault).search_text("concise").matches == ()


def test_conflicts_and_inferences_are_not_presented_as_explicit_facts(tmp_path: Path) -> None:
    writer, repository, vault = _setup(tmp_path)
    explicit = _record("Telescope calibration is scheduled")
    inference = replace(
        _record("Telescope calibration may be needed", origin=MemoryOrigin.AI_INFERENCE),
        importance=1.0,
        confidence=0.4,
    )
    disputed = _record("Telescope calibration is cancelled")
    pending = _record("Telescope calibration is tomorrow")
    for record in (explicit, inference, disputed, pending):
        writer.submit(record)
    writer.approve(explicit.id)
    writer.approve(inference.id)
    repository.transition(
        disputed.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
    )

    result = MemoryRetriever(repository, vault).search_text("Telescope calibration")
    assert [item.record.id for item in result.matches] == [explicit.id, inference.id]
    assert result.matches[1].record.is_inference
    assert [item.record.id for item in result.conflicts] == [disputed.id]
    assert pending.id not in [item.record.id for item in result.matches]


def test_stale_flag_and_pagination_do_not_hide_later_memories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.memory.retrieval as retrieval

    writer, repository, vault = _setup(tmp_path)
    records = [_record(f"Project alpha detail {number}") for number in range(4)]
    for record in records:
        writer.submit(record)
        writer.approve(record.id)
    monkeypatch.setattr(retrieval, "_PAGE_SIZE", 2)

    future = datetime.now(UTC) + timedelta(days=181)
    result = MemoryRetriever(repository, vault).search_text(
        "Project alpha", limit=10, as_of=future
    )
    assert {item.record.id for item in result.matches} == {record.id for record in records}
    assert all(item.stale for item in result.matches)


def test_missing_or_invalid_vault_note_is_reported_without_snapshot_fallback(
    tmp_path: Path,
) -> None:
    writer, repository, vault = _setup(tmp_path)
    missing = _record("Source alpha")
    invalid = _record("Source beta")
    for record in (missing, invalid):
        writer.submit(record)
        writer.approve(record.id)
    missing_note = vault.read(missing.id)
    invalid_note = vault.read(invalid.id)
    assert missing_note is not None and invalid_note is not None
    missing_note.path.unlink()
    invalid_note.path.write_text(
        invalid_note.path.read_text(encoding="utf-8").replace(
            "confidence: 0.8", 'confidence: "high"'
        ),
        encoding="utf-8",
    )

    result = MemoryRetriever(repository, vault).search_text("Source")
    assert result.matches == ()
    assert {(issue.memory_id, issue.reason) for issue in result.issues} == {
        (missing.id, "missing_note"),
        (invalid.id, "invalid_metadata"),
    }


def test_provenance_edit_needs_review_before_retrieval(tmp_path: Path) -> None:
    writer, repository, vault = _setup(tmp_path)
    record = _record("Owns the observatory key")
    writer.submit(record)
    writer.approve(record.id)
    note = vault.read(record.id)
    assert note is not None
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace(
            'origin: "user_explicit"', 'origin: "ai_inference"'
        ),
        encoding="utf-8",
    )

    result = MemoryRetriever(repository, vault).search_text("observatory")
    assert result.matches == ()
    assert [(issue.memory_id, issue.reason) for issue in result.issues] == [
        (record.id, "invalid_metadata")
    ]


def test_vector_matches_resolve_canonical_records_and_filter_unapproved(tmp_path: Path) -> None:
    writer, repository, vault = _setup(tmp_path)
    approved = _record("Observatory is open")
    conflicted = _record("Observatory is closed")
    pending = _record("Observatory status unknown")
    for record in (approved, conflicted, pending):
        writer.submit(record)
    writer.approve(approved.id)
    repository.transition(
        conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
    )

    class FakeIndex:
        async def search(self, query: VectorQuery) -> list[VectorMatch]:
            assert query.space == "test-model-v1"
            return [
                VectorMatch(str(pending.id), 0.99),
                VectorMatch(str(conflicted.id), 0.9),
                VectorMatch(str(uuid4()), 0.8),
                VectorMatch(str(approved.id), 0.42),
            ]

    query = VectorQuery(space="test-model-v1", values=(0.1, 0.2), limit=2)
    retriever = MemoryRetriever(repository, vault, vector_index=FakeIndex())
    result = asyncio.run(retriever.search_vector(query))
    assert len(result.matches) == 1
    assert result.matches[0].record.id == approved.id
    assert result.matches[0].match_kind == "vector"
    assert result.matches[0].match_score == 0.42
    assert [item.record.id for item in result.conflicts] == [conflicted.id]
    assert result.issues == ()
