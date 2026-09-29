"""The local review entry point keeps publication behind an explicit command."""

import json
from pathlib import Path

from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.review_cli import main


def _candidate() -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.PROJECT,
        content="The migration needs a rollback test.",
        source="review:issue-12",
        origin=MemoryOrigin.TOOL_OBSERVATION,
        importance=0.7,
        confidence=0.9,
        project="jarvis",
    )


def test_list_and_show_are_read_only_until_explicit_approval(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "jarvis.sqlite3"
    database = Database(db_path)
    database.initialize()
    record = _candidate()
    repository = MemoryRepository(database)
    repository.add(record)
    vault_path = tmp_path / "vault"

    assert main(["--db", str(db_path), "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed] == [str(record.id)]
    assert "content" not in listed[0]
    assert not vault_path.exists()

    assert main(["--db", str(db_path), "show", str(record.id)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["content"] == record.content
    assert shown["source"] == record.source
    assert shown["origin"] == "tool_observation"
    assert repository.get(record.id).status is MemoryStatus.PENDING

    assert main(
        [
            "--db", str(db_path), "approve", str(record.id),
            "--vault", str(vault_path), "--actor", "reviewer:alice",
        ]
    ) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"
    assert repository.get(record.id).status is MemoryStatus.APPROVED
    assert ObsidianVault(vault_path).read(record.id).body == record.content
    assert main(["--db", str(db_path), "history", str(record.id)]) == 0
    events = json.loads(capsys.readouterr().out)
    assert [(event["action"], event["actor"]) for event in events] == [
        ("approve", "reviewer:alice")
    ]
    assert events[0]["vault_revision"] == repository.get(record.id).vault_revision


def test_conflict_and_rejection_require_review_and_do_not_write_note(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "jarvis.sqlite3"
    database = Database(db_path)
    database.initialize()
    record = _candidate()
    repository = MemoryRepository(database)
    repository.add(record)
    vault_path = tmp_path / "vault"

    assert main(
        ["--db", str(db_path), "flag-conflict", str(record.id), "--actor", "reviewer:bob"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "conflict"
    assert main(["--db", str(db_path), "list", "--status", "conflict"]) == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)] == [str(record.id)]
    assert main(["--db", str(db_path), "reject", str(record.id), "--actor", "reviewer:bob"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
    assert not vault_path.exists()

    assert main(
        [
            "--db", str(db_path), "approve", str(record.id),
            "--vault", str(vault_path), "--actor", "reviewer:bob",
        ]
    ) == 1
    assert "Rejected" in capsys.readouterr().err
    assert repository.get(record.id).status is MemoryStatus.REJECTED
    assert not vault_path.exists()
    assert main(["--db", str(db_path), "history", str(record.id)]) == 0
    events = json.loads(capsys.readouterr().out)
    assert [(event["action"], event["actor"]) for event in events] == [
        ("flag_conflict", "reviewer:bob"),
        ("reject", "reviewer:bob"),
    ]


def test_missing_database_and_candidate_do_not_create_storage(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "missing.sqlite3"
    assert main(["--db", str(db_path), "list"]) == 1
    assert "not ready" in capsys.readouterr().err
    assert not db_path.exists()

    Database(db_path).initialize()
    unknown = _candidate().id
    assert main(["--db", str(db_path), "show", str(unknown)]) == 1
    assert "not found" in capsys.readouterr().err


def test_approved_memory_cannot_be_rejected_by_review_cli(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "jarvis.sqlite3"
    database = Database(db_path)
    database.initialize()
    record = _candidate()
    repository = MemoryRepository(database)
    repository.add(record)
    vault_path = tmp_path / "vault"
    assert main(
        [
            "--db", str(db_path), "approve", str(record.id),
            "--vault", str(vault_path), "--actor", "reviewer:alice",
        ]
    ) == 0
    capsys.readouterr()

    assert main(["--db", str(db_path), "reject", str(record.id), "--actor", "reviewer:alice"]) == 1
    assert "Invalid memory state transition" in capsys.readouterr().err
    assert repository.get(record.id).status is MemoryStatus.APPROVED
    assert ObsidianVault(vault_path).read(record.id).body == record.content
