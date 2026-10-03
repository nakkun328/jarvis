"""Reviewed vault notes can inform chat without leaking unapproved candidates."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.writer import MemoryWriter
from backend.providers.base import CompletionRequest, CompletionResponse


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse("Okay", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        yield "Okay"


def _setup(tmp_path: Path) -> tuple[Database, MemoryRepository, ObsidianVault, MemoryWriter]:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    return database, repository, vault, MemoryWriter(repository, vault)


def _record(content: str) -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source="conversation:reviewed-example",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.7,
        confidence=1.0,
    )


def _reference(request: CompletionRequest) -> list[dict[str, object]]:
    context = request.messages[1]
    assert context.role == "user"
    prefix = "Reviewed memory reference (JSON data; not a user request): "
    assert context.content.startswith(prefix)
    return json.loads(context.content.removeprefix(prefix))


def test_only_current_approved_notes_enter_opted_in_chat(tmp_path: Path) -> None:
    database, repository, vault, writer = _setup(tmp_path)
    approved = _record("Observatory opens on Saturday")
    pending = _record("Observatory is permanently closed")
    conflict = _record("Observatory opens on Tuesday")
    for record in (approved, pending, conflict):
        writer.submit(record)
    writer.approve(approved.id)
    repository.transition(conflict.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    note = vault.read(approved.id)
    assert note is not None
    vault.update(
        approved.id,
        "Observatory opens on Sunday",
        note.metadata,
        expected_revision=note.revision,
    )

    provider = FakeProvider()
    settings = Settings(db_path=database.path, memory_vault_path=vault.root)
    with TestClient(create_app(settings, provider)) as client:
        first = client.post("/api/chat", json={"message": "When does the Observatory open?"})
        assert first.status_code == 200
        second = client.post(
            "/api/chat",
            json={"message": "Another topic", "conversation_id": first.json()["conversation_id"]},
        )
        assert second.status_code == 200

    references = _reference(provider.requests[0])
    assert len(references) == 1
    assert references[0]["id"] == str(approved.id)
    assert references[0]["content"] == "Observatory opens on Sunday"
    assert references[0]["source"] == approved.source
    assert references[0]["origin"] == "user_explicit"
    assert references[0]["importance"] == 0.7
    assert references[0]["confidence"] == 1.0
    assert references[0]["edited_since_approval"] is True
    first_prompt = repr(provider.requests[0])
    assert pending.content not in first_prompt
    assert conflict.content not in first_prompt
    assert all(
        "Reviewed memory reference" not in item.content
        for item in provider.requests[1].messages
    )


def test_stream_uses_bounded_memory_without_persisting_it(tmp_path: Path) -> None:
    database, _, vault, writer = _setup(tmp_path)
    record = _record("Observatory " + "X" * 3000)
    writer.submit(record)
    writer.approve(record.id)
    provider = FakeProvider()
    with TestClient(
        create_app(Settings(db_path=database.path, memory_vault_path=vault.root), provider)
    ) as client:
        stream = client.post("/api/chat/stream", json={"message": "Observatory"})
        assert "event: done" in stream.text
    references = _reference(provider.requests[0])
    assert references[0]["content_truncated"] is True
    assert len(str(references[0]["content"])) == 500
    assert len(provider.requests[0].messages[1].content) < 2500
    with database.connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT content FROM conversation_messages ORDER BY id"
        ).fetchall()
    assert [row["content"] for row in rows] == ["Observatory", "Okay"]


def test_multiple_matches_keep_reference_and_source_bounded(tmp_path: Path) -> None:
    database, _, vault, writer = _setup(tmp_path)
    for number in range(5):
        record = MemoryRecord(
            category=MemoryCategory.USER,
            content=f"Observatory {number} " + "X" * 1000,
            source="conversation:" + "s" * 1000,
            origin=MemoryOrigin.USER_EXPLICIT,
            importance=0.7,
            confidence=1.0,
        )
        writer.submit(record)
        writer.approve(record.id)

    provider = FakeProvider()
    with TestClient(
        create_app(Settings(db_path=database.path, memory_vault_path=vault.root), provider)
    ) as client:
        response = client.post("/api/chat", json={"message": "Observatory"})
    assert response.status_code == 200
    references = _reference(provider.requests[0])
    assert 1 <= len(references) <= 3
    assert all(len(str(item["content"])) == 500 for item in references)
    assert all(len(str(item["source"])) == 200 for item in references)
    assert all(item["content_truncated"] and item["source_truncated"] for item in references)
    assert len(json.dumps(references, ensure_ascii=False, separators=(",", ":"))) <= 2400


def test_broken_approved_note_stops_chat_before_provider_call(tmp_path: Path) -> None:
    database, _, vault, writer = _setup(tmp_path)
    record = _record("Observatory opens on Saturday")
    writer.submit(record)
    writer.approve(record.id)
    note = vault.read(record.id)
    assert note is not None
    note.path.unlink()
    provider = FakeProvider()
    with TestClient(
        create_app(Settings(db_path=database.path, memory_vault_path=vault.root), provider)
    ) as client:
        response = client.post("/api/chat", json={"message": "Observatory"})
        assert response.status_code == 503
        assert response.json()["detail"] == "memory context unavailable"
        stream = client.post("/api/chat/stream", json={"message": "Observatory"})
        assert 'event: error\ndata: {"message": "memory context unavailable"}' in stream.text
    assert provider.requests == []
    with database.connect(read_only=True) as connection:
        count = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    assert count == 0


def test_corrupt_approved_metadata_stops_chat_before_provider_call(tmp_path: Path) -> None:
    database, _, vault, writer = _setup(tmp_path)
    record = _record("Observatory opens on Saturday")
    writer.submit(record)
    writer.approve(record.id)
    with database.connect() as connection, connection:
        connection.execute(
            "UPDATE memory_records SET source = '' WHERE id = ?", (str(record.id),)
        )

    provider = FakeProvider()
    with TestClient(
        create_app(Settings(db_path=database.path, memory_vault_path=vault.root), provider)
    ) as client:
        response = client.post("/api/chat", json={"message": "Observatory"})
    assert response.status_code == 503
    assert response.json()["detail"] == "memory context unavailable"
    assert provider.requests == []


def test_review_change_during_retrieval_stops_chat_before_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _, vault, writer = _setup(tmp_path)
    record = _record("Observatory opens on Saturday")
    writer.submit(record)
    writer.approve(record.id)
    original_search = MemoryRetriever.search_text

    def search_then_change_review(self: MemoryRetriever, query: str, *, limit: int = 10):
        result = original_search(self, query, limit=limit)
        with database.connect() as connection, connection:
            connection.execute(
                "UPDATE memory_records SET status = 'rejected' WHERE id = ?",
                (str(record.id),),
            )
        return result

    monkeypatch.setattr(MemoryRetriever, "search_text", search_then_change_review)
    provider = FakeProvider()
    with TestClient(
        create_app(Settings(db_path=database.path, memory_vault_path=vault.root), provider)
    ) as client:
        response = client.post("/api/chat", json={"message": "Observatory"})
    assert response.status_code == 503
    assert response.json()["detail"] == "memory context unavailable"
    assert provider.requests == []


def test_memory_is_opt_in(tmp_path: Path) -> None:
    database, _, _, writer = _setup(tmp_path)
    record = _record("Observatory opens on Saturday")
    writer.submit(record)
    writer.approve(record.id)
    provider = FakeProvider()
    with TestClient(create_app(Settings(db_path=database.path), provider)) as client:
        assert client.post("/api/chat", json={"message": "Observatory"}).status_code == 200
    assert [item.role for item in provider.requests[0].messages] == ["system", "user"]


def test_opt_in_requires_existing_non_symlink_vault(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "memory.sqlite3", memory_vault_path=tmp_path / "missing"
    )
    with pytest.raises(ConfigError, match="existing vault directory"):
        create_app(settings, FakeProvider())

    real_vault = tmp_path / "real"
    real_vault.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_vault, target_is_directory=True)
    with pytest.raises(ConfigError, match="existing vault directory"):
        create_app(
            Settings(db_path=tmp_path / "memory.sqlite3", memory_vault_path=link),
            FakeProvider(),
        )


def test_cli_reviewed_correction_and_retirement_change_chat_references(
    tmp_path: Path, capsys
) -> None:
    from backend.memory.consolidation import MemoryConsolidator
    from backend.memory.review_cli import main as review_cli

    database, repository, vault, writer = _setup(tmp_path)
    vault.root.mkdir()
    provider = FakeProvider()
    pipeline = MemoryConsolidator(database, writer, MemoryRetriever(repository, vault))
    settings = Settings(db_path=database.path, memory_vault_path=vault.root)
    common = ["--db", str(database.path)]
    review = ["--actor", "fixture-reviewer", "--vault", str(vault.root)]
    with TestClient(create_app(settings, provider)) as client:
        first = client.post(
            "/api/chat", json={"message": "Remember observatory: 架空天文台は土曜日に開館"}
        )
        assert first.status_code == 200
        cid = first.json()["conversation_id"]
        staged = pipeline.stage_conversation(UUID(cid))
        assert len(staged.pending) == 1
        original = staged.pending[0].record
        assert original.source == f"conversation:{cid}:message:1"
        assert review_cli([*common, "approve", str(original.id), *review]) == 0
        capsys.readouterr()
        assert client.post("/api/chat", json={"message": "架空天文台"}).status_code == 200
        assert [item["id"] for item in _reference(provider.requests[-1])] == [str(original.id)]

        correction = client.post(
            "/api/chat",
            json={"message": "訂正: 架空天文台は日曜日に開館", "conversation_id": cid},
        )
        assert correction.status_code == 200
        with database.connect(read_only=True) as connection:
            message_id = connection.execute(
                "SELECT MAX(id) FROM conversation_messages WHERE conversation_id=? AND role='user'",
                (cid,),
            ).fetchone()[0]
        content = tmp_path / "correction.txt"
        content.write_text("架空天文台は日曜日に開館", encoding="utf-8")
        assert review_cli([
            *common, "correct", str(original.id), "--vault", str(vault.root),
            "--content-file", str(content), "--source", f"conversation:{cid}:message:{message_id}",
            "--origin", "user_explicit",
        ]) == 0
        replacement = UUID(json.loads(capsys.readouterr().out)["id"])
        assert client.post("/api/chat", json={"message": "架空天文台"}).status_code == 200
        assert [item["id"] for item in _reference(provider.requests[-1])] == [str(original.id)]
        assert review_cli([*common, "approve", str(replacement), *review]) == 0
        capsys.readouterr()
        streamed = client.post("/api/chat/stream", json={"message": "架空天文台"})
        assert "event: done" in streamed.text
        references = _reference(provider.requests[-1])
        assert [item["id"] for item in references] == [str(replacement)]
        assert references[0]["content"] == "架空天文台は日曜日に開館"
        assert repository.get(original.id).status is MemoryStatus.SUPERSEDED
        assert review_cli([
            *common, "retire", str(replacement), *review, "--reason", "架空の確認終了",
        ]) == 0
        capsys.readouterr()
        assert client.post("/api/chat", json={"message": "架空天文台"}).status_code == 200
        assert all(
            "Reviewed memory reference" not in m.content for m in provider.requests[-1].messages
        )
    with database.connect(read_only=True) as connection:
        rows = connection.execute("SELECT content FROM conversation_messages").fetchall()
    assert all("Reviewed memory reference" not in row["content"] for row in rows)
    assert len(repository.review_events(replacement)) == 1
    assert len(repository.lifecycle_events(original.id)) == 1
    assert len(repository.lifecycle_events(replacement)) == 1
