"""Self memory records lessons without bypassing human review."""

from pathlib import Path

import pytest

from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.self_memory import SelfMemoryKind, SelfMemoryRecorder
from backend.memory.writer import MemoryWriter


@pytest.mark.parametrize(
    ("kind", "prefix", "guidance_label"),
    [
        (SelfMemoryKind.SUCCESS, "Succeeded", "Repeat"),
        (SelfMemoryKind.FAILURE, "Failed", "Next time"),
        (SelfMemoryKind.CORRECTION, "Correction", "Use instead"),
    ],
)
def test_self_memory_stays_pending_until_review(
    tmp_path: Path, kind: SelfMemoryKind, prefix: str, guidance_label: str
) -> None:
    database = Database(tmp_path / "jarvis.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    recorder = SelfMemoryRecorder(writer)

    candidate = recorder.record(
        kind=kind,
        observation="The first attempt missed a requirement.",
        guidance="Check the acceptance criteria before reporting completion.",
        source="conversation:turn-42",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
        tags=("workflow",),
        project="jarvis",
    )

    assert candidate.status is MemoryStatus.PENDING
    assert candidate.record.category is MemoryCategory.SELF
    assert candidate.record.content == (
        f"{prefix}: The first attempt missed a requirement.\n"
        f"{guidance_label}: Check the acceptance criteria before reporting completion."
    )
    assert candidate.record.tags == (f"self-kind:{kind.value}", "workflow")
    assert candidate.record.source == "conversation:turn-42"
    assert candidate.record.origin is MemoryOrigin.USER_EXPLICIT
    assert candidate.record.project == "jarvis"
    assert MemoryRepository(database).get(candidate.record.id) == candidate
    assert not vault.root.exists()

    approved = writer.approve(candidate.record.id)
    assert approved.status is MemoryStatus.APPROVED
    assert vault.read(candidate.record.id).body == candidate.record.content


def test_correction_is_new_candidate_and_does_not_change_earlier_memory(tmp_path: Path) -> None:
    database = Database(tmp_path / "jarvis.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    recorder = SelfMemoryRecorder(writer)
    earlier = recorder.record(
        kind=SelfMemoryKind.SUCCESS,
        observation="Sent a short answer.",
        guidance="Keep answers short.",
        source="conversation:turn-1",
        origin=MemoryOrigin.AI_INFERENCE,
        importance=0.4,
        confidence=0.5,
    )
    writer.approve(earlier.record.id)
    original_note = vault.read(earlier.record.id)

    correction = recorder.record(
        kind=SelfMemoryKind.CORRECTION,
        observation="The answer omitted required context.",
        guidance="Include the reason when explaining a change.",
        source="conversation:turn-2",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.9,
        confidence=1.0,
    )

    assert correction.status is MemoryStatus.PENDING
    assert repository.get(earlier.record.id).status is MemoryStatus.APPROVED
    assert vault.read(earlier.record.id) == original_note
    assert vault.read(correction.record.id) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"observation": "  "},
        {"guidance": "\n"},
        {"kind": "success"},
        {"tags": ("self-kind:failure",)},
    ],
)
def test_invalid_self_memory_does_not_write(tmp_path: Path, overrides: dict[str, object]) -> None:
    database = Database(tmp_path / "jarvis.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    recorder = SelfMemoryRecorder(MemoryWriter(repository, ObsidianVault(tmp_path / "vault")))
    values = {
        "kind": SelfMemoryKind.SUCCESS,
        "observation": "A result",
        "guidance": "Repeat the useful step",
        "source": "tool:run-1",
        "origin": MemoryOrigin.TOOL_OBSERVATION,
        "importance": 0.5,
        "confidence": 0.8,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        recorder.record(**values)
    assert repository.list_by_status(MemoryStatus.PENDING) == []
