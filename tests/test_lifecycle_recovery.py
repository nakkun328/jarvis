"""Reproduce late audit failure, simultaneous review and canonical recovery."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest

from backend.chat.persistence import SQLiteConversationStore
from backend.chat.service import ChatService
from backend.core.database import Database
from backend.memory.consolidation import MemoryConsolidator
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryRepositoryError, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.review_cli import main as review_cli
from backend.memory.writer import MemoryWriteConflict, MemoryWriter
from backend.providers.base import CompletionResponse


@pytest.fixture
def system(tmp_path: Path):
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    return database, repository, vault, writer, MemoryRetriever(repository, vault)


def _record(content: str) -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source="conversation:fixture",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
    )


def test_late_correction_audit_failure_rolls_back_lifecycle_and_retries_once(system):
    database, repository, vault, writer, retriever = system
    original, correction = _record("旧回答"), _record("訂正回答")
    writer.submit(original)
    before = writer.approve(original.id, actor="first-reviewer")
    writer.submit_correction(original.id, correction)
    # This fails after both row updates AND the supersession event were executed.
    with database.connect() as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_late_audit BEFORE INSERT ON memory_review_events "
            "WHEN NEW.memory_id = '" + str(correction.id) + "' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    with pytest.raises(MemoryRepositoryError):
        writer.approve(correction.id, actor="correcting-reviewer")
    note = vault.read(correction.id)
    assert note is not None
    assert repository.get(original.id) == before
    assert repository.get(correction.id).status is MemoryStatus.PENDING
    assert repository.lifecycle_events(original.id) == []
    assert repository.review_events(correction.id) == []
    assert retriever.get_approved(correction.id) is None
    with database.connect() as connection, connection:
        connection.execute("DROP TRIGGER fail_late_audit")
    for _ in range(2):
        writer.approve(correction.id, actor="correcting-reviewer")
    assert vault.read(correction.id) == note
    assert repository.get(original.id).replaced_by_id == correction.id
    assert retriever.get_approved(original.id) is None
    assert len(repository.lifecycle_events(original.id)) == 1
    assert len(repository.review_events(correction.id)) == 1


@pytest.mark.parametrize("winner", ["correct", "retire"])
def test_simultaneous_correction_and_retirement_have_one_committed_winner(system, winner):
    database, repository, vault, writer, retriever = system
    original, correction = _record("旧回答"), _record("訂正回答")
    writer.submit(original)
    writer.approve(original.id, actor="first-reviewer")
    writer.submit_correction(original.id, correction)
    barrier = threading.Barrier(2)
    committed = threading.Event()

    class RacingRepository(MemoryRepository):
        def ordered_commit(self, action, operation, *args, **kwargs):
            barrier.wait(timeout=10)
            if action != winner:
                assert committed.wait(timeout=10)
            try:
                return operation(*args, **kwargs)
            finally:
                if action == winner:
                    committed.set()

        def transition(self, *args, **kwargs):
            return self.ordered_commit("correct", super().transition, *args, **kwargs)

        def retire(self, *args, **kwargs):
            return self.ordered_commit("retire", super().retire, *args, **kwargs)

    def review(action):
        racing_writer = MemoryWriter(RacingRepository(database), vault)
        try:
            if action == "correct":
                return racing_writer.approve(correction.id, actor="corrector").status
            return racing_writer.retire(original.id, actor="retirer", reason="obsolete").status
        except MemoryWriteConflict:
            return "conflict"

    # Both resolve the approved original before SQLite CAS; exercise both commit orders.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(review, ("correct", "retire")))
    assert results.count("conflict") == 1
    assert results[0 if winner == "retire" else 1] == "conflict"
    assert len(repository.lifecycle_events(original.id)) == 1
    assert retriever.get_approved(original.id) is None
    if MemoryStatus.APPROVED in results:
        assert repository.get(original.id).status is MemoryStatus.SUPERSEDED
        assert repository.get(original.id).replaced_by_id == correction.id
        assert [item.record.id for item in retriever.search_text("訂正回答").matches] == [
            correction.id
        ]
        assert len(repository.review_events(correction.id)) == 1
    else:
        assert repository.get(original.id).status is MemoryStatus.RETIRED
        assert repository.get(correction.id).status is MemoryStatus.PENDING
        assert repository.review_events(correction.id) == []
        assert retriever.search_text("訂正回答").matches == ()


def _probe(database_path: Path, vault_path: Path) -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root) + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "tests/fixtures/lifecycle_recovery_probe.py"),
            str(database_path),
            str(vault_path),
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_conversation_cli_correction_wal_backup_and_fresh_process_restore(system, tmp_path, capsys):
    database, repository, vault, writer, retriever = system

    class FakeProvider:
        name = "fake"
        model = "fake"

        async def complete(self, request):
            return CompletionResponse("了解しました", self.name, self.model)

    service = ChatService(FakeProvider(), SQLiteConversationStore(database))
    turn = asyncio.run(service.complete("Remember reply_style: 短い回答が好き"))
    pipeline = MemoryConsolidator(database, writer, retriever)
    staged = pipeline.stage_conversation(turn.conversation_id)
    original = staged.pending[0].record
    assert len(staged.pending) == 1
    assert original.source == f"conversation:{turn.conversation_id}:message:1"
    assert retriever.get_approved(original.id) is None
    common = ["--db", str(database.path)]
    approve = ["--vault", str(vault.root), "--actor", "reviewer"]
    assert review_cli([*common, "approve", str(original.id), *approve]) == 0
    capsys.readouterr()
    assert [item.record.id for item in retriever.search_text("短い回答").matches] == [original.id]

    asyncio.run(service.complete("今は詳しい回答が好き", turn.conversation_id))
    content = tmp_path / "correction.txt"
    content.write_text("詳しい回答が好き", encoding="utf-8")
    assert (
        review_cli(
            [
                *common,
                "correct",
                str(original.id),
                "--vault",
                str(vault.root),
                "--content-file",
                str(content),
                "--source",
                f"conversation:{turn.conversation_id}:message:3",
                "--origin",
                "user_explicit",
            ]
        )
        == 0
    )
    correction_id = UUID(json.loads(capsys.readouterr().out)["id"])
    assert review_cli([*common, "approve", str(correction_id), *approve]) == 0
    capsys.readouterr()
    assert retriever.search_text("短い回答").matches == ()
    assert [item.record.id for item in retriever.search_text("詳しい回答").matches] == [
        correction_id
    ]
    retired = _record("退役した回答")
    writer.submit(retired)
    writer.approve(retired.id, actor="reviewer")
    writer.retire(retired.id, actor="reviewer", reason="obsolete")
    pending = _record("未承認の回答")
    writer.submit(pending)

    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir(mode=0o700)
    # Writers/editors are quiesced for both DB and vault snapshot operations.
    with database.connect() as source:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        with source:
            source.execute("UPDATE conversations SET updated_at='committed-wal-marker'")
        assert Path(str(database.path) + "-wal").exists()
        before = _probe(database.path, vault.root)
        with closing(sqlite3.connect(snapshot_dir / "memory.sqlite3")) as target:
            source.backup(target)
    shutil.copytree(vault.root, snapshot_dir / "vault")
    restore_dir = tmp_path / "restored"
    shutil.copytree(snapshot_dir, restore_dir)
    for _ in range(2):
        restored = _probe(restore_dir / "memory.sqlite3", restore_dir / "vault")
        assert restored == before  # Includes all tables, audit rows, revisions and current content.
    assert [item["id"] for item in restored["canonical"]] == [str(correction_id)]
    assert restored["tables"]["conversations"][0]["updated_at"] == "committed-wal-marker"
    assert len(restored["tables"]["conversation_messages"]) == 4
    assert len(repository.review_events(correction_id)) == 1
