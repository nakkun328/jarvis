"""History-preserving correction and retirement across SQLite and the vault."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from backend.core.database import Database
from backend.memory.consolidation import IndexRefreshError, MemoryConsolidator, SelfEvent
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import (
    MemoryRepository,
    MemoryRepositoryError,
    MemoryStatus,
)
from backend.memory.retrieval import MemoryRetriever
from backend.memory.review_cli import main
from backend.memory.self_memory import SelfMemoryKind, SelfMemoryRecorder
from backend.memory.writer import MemoryWriteConflict, MemoryWriter


def _system(tmp_path: Path) -> tuple[MemoryWriter, MemoryRepository, MemoryRetriever]:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    return writer, repository, MemoryRetriever(repository, vault)


def _record(
    content: str = "Prefers short replies", *, category=MemoryCategory.USER
) -> MemoryRecord:
    return MemoryRecord(
        category=category,
        content=content,
        source="conversation:original",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
    )


def test_correction_requires_separate_review_and_keeps_old_note(tmp_path: Path) -> None:
    writer, repository, retriever = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id, actor="reviewer:a")
    correction = replace(_record("Prefers detailed replies"), source="user:correction")
    staged = writer.submit_correction(original.id, correction)
    assert staged.status is MemoryStatus.PENDING
    assert staged.supersedes_id == original.id
    assert staged.supersedes_revision == writer.vault.read(original.id).revision
    assert repository.get(original.id).status is MemoryStatus.APPROVED
    assert [item.record.id for item in retriever.search_text("short").matches] == [original.id]
    assert retriever.get_approved(correction.id) is None

    approved = writer.approve(correction.id, actor="reviewer:b")
    assert approved.status is MemoryStatus.APPROVED
    assert repository.get(original.id).status is MemoryStatus.SUPERSEDED
    assert repository.get(original.id).replaced_by_id == correction.id
    assert writer.vault.read(original.id).body == original.content
    assert retriever.get_approved(original.id) is None
    assert [item.record.id for item in retriever.search_text("detailed").matches] == [correction.id]
    assert retriever.search_text("short").matches == ()
    assert [
        (event.action, event.related_id, event.actor)
        for event in repository.lifecycle_events(original.id)
    ] == [("supersede", correction.id, "reviewer:b")]
    assert [(event.action, event.actor) for event in repository.review_events(correction.id)] == [
        ("approve", "reviewer:b")
    ]
    assert (
        MemoryWriter(MemoryRepository(repository.database), writer.vault).approve(
            correction.id, actor="reviewer:b"
        )
        == approved
    )
    assert len(repository.lifecycle_events(original.id)) == 1


def test_competing_corrections_cannot_both_replace_original(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    first = _record("First correction")
    second = _record("Second correction")
    writer.submit_correction(original.id, first)
    writer.submit_correction(original.id, second)
    writer.approve(first.id)
    with pytest.raises(MemoryWriteConflict, match="Original memory changed"):
        writer.approve(second.id)
    assert repository.get(second.id).status is MemoryStatus.PENDING
    assert repository.get(original.id).replaced_by_id == first.id
    assert len(repository.lifecycle_events(original.id)) == 1


def test_human_edit_to_original_blocks_stale_correction(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    correction = _record("Replacement")
    writer.submit_correction(original.id, correction)
    note = writer.vault.read(original.id)
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace(original.content, "Human update"),
        encoding="utf-8",
    )
    with pytest.raises(MemoryWriteConflict, match="Original memory changed"):
        writer.approve(correction.id)
    assert repository.get(original.id).status is MemoryStatus.APPROVED
    assert repository.get(correction.id).status is MemoryStatus.PENDING


def test_correction_staging_rejects_edit_after_canonical_read(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    observed_revision = writer.vault.read(original.id).revision
    note = writer.vault.read(original.id)
    writer.vault.update(
        original.id,
        "Human-edited preference",
        note.metadata,
        expected_revision=observed_revision,
    )
    correction = _record("Replacement")
    with pytest.raises(MemoryWriteConflict, match="changed while staging"):
        writer.submit_correction(original.id, correction, expected_old_revision=observed_revision)
    assert repository.get(correction.id) is None
    assert repository.get(original.id).status is MemoryStatus.APPROVED


def test_audit_failure_rolls_back_both_halves_of_correction(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    correction = _record("Replacement")
    writer.submit_correction(original.id, correction)
    with repository.database.connect() as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_lifecycle BEFORE INSERT ON memory_lifecycle_events "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    with pytest.raises(MemoryRepositoryError, match="unavailable"):
        writer.approve(correction.id)
    assert repository.get(original.id).status is MemoryStatus.APPROVED
    assert repository.get(correction.id).status is MemoryStatus.PENDING
    assert repository.review_events(correction.id) == []
    with repository.database.connect() as connection, connection:
        connection.execute("DROP TRIGGER fail_lifecycle")
    assert writer.approve(correction.id).status is MemoryStatus.APPROVED


def test_retirement_preserves_note_and_excludes_retrieval(tmp_path: Path) -> None:
    writer, repository, retriever = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    retired = writer.retire(original.id, actor="reviewer:c", reason="Outdated preference")
    assert retired.status is MemoryStatus.RETIRED
    assert writer.vault.read(original.id).body == original.content
    assert retriever.get_approved(original.id) is None
    assert retriever.search_text("short").matches == ()
    assert repository.list_by_status(MemoryStatus.RETIRED) == [retired]
    events = repository.lifecycle_events(original.id)
    assert [(event.action, event.actor, event.reason) for event in events] == [
        ("retire", "reviewer:c", "Outdated preference")
    ]
    with pytest.raises(MemoryWriteConflict):
        writer.retire(original.id, actor="reviewer:c", reason="retry")
    assert len(repository.lifecycle_events(original.id)) == 1


def test_retirement_racing_with_correction_blocks_stale_approval(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    correction = _record("Replacement")
    writer.submit_correction(original.id, correction)
    writer.retire(original.id, actor="reviewer:a", reason="No longer true")
    with pytest.raises(MemoryWriteConflict, match="Original memory changed"):
        writer.approve(correction.id)
    assert repository.get(correction.id).status is MemoryStatus.PENDING
    assert repository.get(original.id).status is MemoryStatus.RETIRED


def test_retirement_audit_failure_rolls_back_state(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    with repository.database.connect() as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_retirement BEFORE INSERT ON memory_lifecycle_events "
            "WHEN NEW.action = 'retire' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    with pytest.raises(MemoryRepositoryError, match="unavailable"):
        writer.retire(original.id, actor="reviewer:a", reason="No longer true")
    assert repository.get(original.id).status is MemoryStatus.APPROVED
    assert repository.lifecycle_events(original.id) == []


def test_retirement_rejects_control_characters_in_audit_reason(tmp_path: Path) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    with pytest.raises(ValueError, match="printable text"):
        writer.retire(original.id, actor="reviewer:a", reason="outdated\nforged log line")
    assert repository.get(original.id).status is MemoryStatus.APPROVED
    assert repository.lifecycle_events(original.id) == []


def test_self_correction_is_staged_against_prior_self_memory(tmp_path: Path) -> None:
    writer, repository, retriever = _system(tmp_path)
    recorder = SelfMemoryRecorder(writer)
    original = recorder.record(
        kind=SelfMemoryKind.FAILURE,
        observation="Missed a test",
        guidance="Run unit tests",
        source="run:1",
        origin=MemoryOrigin.TOOL_OBSERVATION,
        importance=0.7,
        confidence=0.9,
    )
    writer.approve(original.record.id)
    correction = recorder.record(
        kind=SelfMemoryKind.CORRECTION,
        observation="Test scope was incomplete",
        guidance="Run integration tests too",
        source="run:2",
        origin=MemoryOrigin.TOOL_OBSERVATION,
        importance=0.8,
        confidence=0.9,
        supersedes_id=original.record.id,
    )
    assert correction.supersedes_id == original.record.id
    writer.approve(correction.record.id)
    assert repository.get(original.record.id).status is MemoryStatus.SUPERSEDED
    assert retriever.get_approved(original.record.id) is None


def test_cli_correct_then_approve_and_retire(tmp_path: Path, capsys) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    db = str(repository.database.path)
    vault = str(writer.vault.root)
    assert (
        main(["--db", db, "approve", str(original.id), "--vault", vault, "--actor", "alice"]) == 0
    )
    capsys.readouterr()
    text_path = tmp_path / "correction.txt"
    text_path.write_text("Now prefers detailed replies", encoding="utf-8")
    assert (
        main(
            [
                "--db",
                db,
                "correct",
                str(original.id),
                "--vault",
                vault,
                "--content-file",
                str(text_path),
                "--source",
                "user:latest",
                "--origin",
                "user_explicit",
            ]
        )
        == 0
    )
    staged = __import__("json").loads(capsys.readouterr().out)
    new_id = UUID(staged["id"])
    assert staged["supersedes_id"] == str(original.id)
    assert repository.get(original.id).status is MemoryStatus.APPROVED
    assert main(["--db", db, "approve", str(new_id), "--vault", vault, "--actor", "alice"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                db,
                "retire",
                str(new_id),
                "--vault",
                vault,
                "--actor",
                "alice",
                "--reason",
                "No longer relevant",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert repository.get(new_id).status is MemoryStatus.RETIRED
    assert main(["--db", db, "history", str(original.id)]) == 0
    events = __import__("json").loads(capsys.readouterr().out)
    assert [event["action"] for event in events] == ["approve", "supersede"]


def test_cli_correction_inherits_current_vault_metadata(tmp_path: Path, capsys) -> None:
    writer, repository, _ = _system(tmp_path)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    note = writer.vault.read(original.id)
    writer.vault.update(
        original.id,
        note.body,
        {
            **note.metadata,
            "importance": 0.35,
            "confidence": 0.7,
            "tags": ["current-preference"],
            "project": "active-project",
        },
        expected_revision=note.revision,
    )
    text_path = tmp_path / "correction.txt"
    text_path.write_text("Prefers detailed replies", encoding="utf-8")
    assert (
        main(
            [
                "--db",
                str(repository.database.path),
                "correct",
                str(original.id),
                "--vault",
                str(writer.vault.root),
                "--content-file",
                str(text_path),
                "--source",
                "user:latest",
                "--origin",
                "user_explicit",
            ]
        )
        == 0
    )
    staged_id = UUID(__import__("json").loads(capsys.readouterr().out)["id"])
    staged = repository.get(staged_id)
    assert staged.record.importance == 0.35
    assert staged.record.confidence == 0.7
    assert staged.record.tags == ("current-preference",)
    assert staged.record.project == "active-project"
    assert staged.supersedes_revision == writer.vault.read(original.id).revision


def test_consolidation_retries_correction_and_retirement_index_cleanup(tmp_path: Path) -> None:
    writer, repository, retriever = _system(tmp_path)

    class FlakyIndex:
        def __init__(self) -> None:
            self.refreshed: list[UUID] = []
            self.removed: list[UUID] = []
            self.fail_once = True

        def refresh(self, memory) -> None:
            self.refreshed.append(memory.record.id)

        def remove_inactive(self, memory_id: UUID) -> None:
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("private provider body")
            self.removed.append(memory_id)

    index = FlakyIndex()
    pipeline = MemoryConsolidator(repository.database, writer, retriever, index_refresher=index)
    original = _record()
    writer.submit(original)
    writer.approve(original.id)
    correction = _record("Replacement")
    writer.submit_correction(original.id, correction)
    with pytest.raises(IndexRefreshError, match="retry publish_reviewed") as failure:
        pipeline.publish_reviewed(correction.id, actor="alice")
    assert "private provider body" not in str(failure.value)
    assert repository.get(original.id).status is MemoryStatus.SUPERSEDED
    assert pipeline.publish_reviewed(correction.id, actor="alice").status is MemoryStatus.APPROVED
    assert index.removed == [original.id]
    index.fail_once = True
    with pytest.raises(IndexRefreshError, match="retry retire_reviewed"):
        pipeline.retire_reviewed(correction.id, actor="alice", reason="Outdated")
    assert repository.get(correction.id).status is MemoryStatus.RETIRED
    assert (
        pipeline.retire_reviewed(correction.id, actor="alice", reason="Outdated").status
        is MemoryStatus.RETIRED
    )
    assert index.removed == [original.id, correction.id]
    assert len(repository.lifecycle_events(correction.id)) == 1


def test_consolidated_self_correction_excludes_retired_history_from_new_conflicts(
    tmp_path: Path,
) -> None:
    writer, repository, retriever = _system(tmp_path)
    original = _record("Failed: old\nNext time: old", category=MemoryCategory.SELF)
    writer.submit(original)
    writer.approve(original.id)
    pipeline = MemoryConsolidator(repository.database, writer, retriever)
    event = SelfEvent(
        event_id="run:2",
        kind=SelfMemoryKind.CORRECTION,
        topic="review",
        observation="Old rule was wrong",
        guidance="Use new rule",
        source="tool:observation",
        origin=MemoryOrigin.TOOL_OBSERVATION,
        importance=0.8,
        confidence=0.9,
        supersedes_id=original.id,
    )
    staged = pipeline.stage([event])
    assert len(staged.conflicts) == 1
    replacement_id = staged.conflicts[0].record.id
    assert staged.conflicts[0].supersedes_id == original.id
    pipeline.publish_reviewed(replacement_id, actor="reviewer:a")
    assert repository.get(original.id).status is MemoryStatus.SUPERSEDED
    assert pipeline.stage([event]).conflicts == ()
