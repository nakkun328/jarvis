"""Reviewed vault notes can inform chat without leaking unapproved candidates."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
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
