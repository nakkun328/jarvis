"""Memory publication across SQLite and the editable Markdown vault."""

from pathlib import Path

import pytest

from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryRepositoryError, MemoryStatus
from backend.memory.writer import MemoryWriteConflict, MemoryWriter


def _record() -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content="Prefers concise replies.",
        source="conversation:123",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
        tags=("communication",),
    )


def _writer(tmp_path: Path) -> tuple[MemoryWriter, MemoryRepository, ObsidianVault]:
    database = Database(tmp_path / "jarvis.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    return MemoryWriter(repository, vault), repository, vault


def test_candidate_is_not_a_fact_until_approved_and_note_survives_restart(tmp_path: Path) -> None:
    writer, repository, vault = _writer(tmp_path)
    candidate = _record()
    submitted = writer.submit(candidate)
    assert submitted.status is MemoryStatus.PENDING
    assert not vault.root.exists()

    approved = writer.approve(candidate.id)
    note = vault.read(candidate.id)
    assert note is not None
    assert note.body == candidate.content
    assert note.metadata["origin"] == "user_explicit"
    assert note.metadata["source"] == candidate.source
    assert note.metadata["confidence"] == 1.0
    assert approved.status is MemoryStatus.APPROVED
    assert approved.vault_revision == note.revision
    assert MemoryRepository(repository.database).get(candidate.id) == approved

    # The approved vault note is human editable; an idempotent retry must not erase it.
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace(candidate.content, "Human correction."),
        encoding="utf-8",
    )
    reopened_writer = MemoryWriter(MemoryRepository(repository.database), vault)
    assert reopened_writer.approve(candidate.id) == approved
    assert vault.read(candidate.id).body == "Human correction."


def test_database_failure_after_note_creation_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, repository, vault = _writer(tmp_path)
    candidate = _record()
    writer.submit(candidate)
    transition = repository.transition

    def fail_once(*args: object, **kwargs: object) -> None:
        raise MemoryRepositoryError("simulated database outage")

    monkeypatch.setattr(repository, "transition", fail_once)
    with pytest.raises(MemoryRepositoryError, match="outage"):
        writer.approve(candidate.id)
    assert repository.get(candidate.id).status is MemoryStatus.PENDING
    assert vault.read(candidate.id) is not None

    monkeypatch.setattr(repository, "transition", transition)
    approved = writer.approve(candidate.id)
    assert approved.status is MemoryStatus.APPROVED
    assert approved.vault_revision == vault.read(candidate.id).revision


def test_retry_refuses_note_edited_after_failed_database_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, repository, vault = _writer(tmp_path)
    candidate = _record()
    writer.submit(candidate)

    def fail_update(*args: object, **kwargs: object) -> None:
        raise MemoryRepositoryError("simulated database outage")

    monkeypatch.setattr(repository, "transition", fail_update)
    with pytest.raises(MemoryRepositoryError):
        writer.approve(candidate.id)
    note = vault.read(candidate.id)
    assert note is not None
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace(candidate.content, "Human correction."),
        encoding="utf-8",
    )

    with pytest.raises(MemoryWriteConflict, match="differs"):
        writer.approve(candidate.id)
    assert repository.get(candidate.id).status is MemoryStatus.PENDING
    assert vault.read(candidate.id).body == "Human correction."


def test_existing_different_note_is_never_overwritten(tmp_path: Path) -> None:
    writer, repository, vault = _writer(tmp_path)
    candidate = _record()
    writer.submit(candidate)
    existing = vault.create(candidate.id, "A human note", {"category": "user"})

    with pytest.raises(MemoryWriteConflict, match="differs"):
        writer.approve(candidate.id)
    assert repository.get(candidate.id).status is MemoryStatus.PENDING
    assert vault.read(candidate.id) == existing


def test_approved_missing_note_is_reported_without_recreation(tmp_path: Path) -> None:
    writer, repository, vault = _writer(tmp_path)
    candidate = _record()
    writer.submit(candidate)
    writer.approve(candidate.id)
    (vault.root / f"{candidate.id}.md").unlink()

    with pytest.raises(MemoryWriteConflict, match="missing"):
        writer.approve(candidate.id)
    assert repository.get(candidate.id).status is MemoryStatus.APPROVED
    assert list(vault.root.glob("*.md")) == []


def test_rejected_and_missing_candidates_cannot_be_published(tmp_path: Path) -> None:
    writer, repository, vault = _writer(tmp_path)
    candidate = _record()
    with pytest.raises(MemoryWriteConflict, match="does not exist"):
        writer.approve(candidate.id)
    writer.submit(candidate)
    repository.transition(
        candidate.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED
    )
    with pytest.raises(MemoryWriteConflict, match="Rejected"):
        writer.approve(candidate.id)
    assert not vault.root.exists()
