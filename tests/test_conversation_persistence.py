"""Conversation persistence, migration, and failed-turn boundaries."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.chat.persistence import SQLiteConversationStore
from backend.chat.service import ChatService
from backend.core.config import Settings
from backend.core.database import SCHEMA_VERSION, Database
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError


class Provider:
    name = "fake"
    model = "fake"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.fail = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.fail:
            raise ProviderError("test failure")
        return CompletionResponse("reply", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        yield "partial"
        if self.fail:
            raise ProviderError("test failure")
        yield " reply"


def test_regular_chat_survives_app_restart_and_keeps_full_transcript(tmp_path: Path) -> None:
    db_path = tmp_path / "chat.sqlite3"
    provider = Provider()
    with TestClient(create_app(Settings(db_path=db_path), provider)) as client:
        first = client.post("/api/chat", json={"message": "first"}).json()
    with TestClient(create_app(Settings(db_path=db_path), provider)) as client:
        second = client.post(
            "/api/chat", json={"message": "second", "conversation_id": first["conversation_id"]}
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == first["conversation_id"]

    assert [item.content for item in provider.requests[-1].messages[1:]] == [
        "first", "reply", "second"
    ]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT role, content FROM conversation_messages ORDER BY id"
        ).fetchall()
    assert rows == [
        ("user", "first"), ("assistant", "reply"),
        ("user", "second"), ("assistant", "reply"),
    ]


def test_stream_survives_restart_and_incomplete_turn_is_not_saved(tmp_path: Path) -> None:
    db_path = tmp_path / "stream.sqlite3"
    provider = Provider()
    with TestClient(create_app(Settings(db_path=db_path), provider)) as client:
        stream = client.post("/api/chat/stream", json={"message": "first"})
        done = json.loads(stream.text.split("event: done\ndata: ", 1)[1].split("\n\n", 1)[0])
        provider.fail = True
        failed = client.post(
            "/api/chat/stream",
            json={"message": "failed", "conversation_id": done["conversation_id"]},
        )
        assert "event: error" in failed.text
        assert "event: done" not in failed.text
    provider.fail = False
    with TestClient(create_app(Settings(db_path=db_path), provider)) as client:
        followup = client.post(
            "/api/chat", json={"message": "next", "conversation_id": done["conversation_id"]}
        )
        assert followup.status_code == 200
    assert [item.content for item in provider.requests[-1].messages[1:]] == [
        "first", "partial reply", "next"
    ]


def test_v1_database_migrates_without_losing_history(tmp_path: Path) -> None:
    db_path = tmp_path / "v1.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00Z')"
        )
        connection.execute("PRAGMA user_version = 1")
    database = Database(db_path)
    database.initialize()
    assert database.is_ready()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()[0] == (1, "2026-01-01T00:00:00Z")
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'conversation_messages'"
        ).fetchone() is not None


def test_unknown_conversation_is_not_created(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.sqlite3"
    with TestClient(create_app(Settings(db_path=db_path), Provider())) as client:
        response = client.post(
            "/api/chat",
            json={"message": "hello", "conversation_id": "7a8567c4-67f5-42ba-ad90-18512f781b1e"},
        )
        assert response.status_code == 404
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_storage_loss_returns_safe_errors_without_recreating_database(tmp_path: Path) -> None:
    db_path = tmp_path / "lost.sqlite3"
    with TestClient(create_app(Settings(db_path=db_path), Provider())) as client:
        first = client.post("/api/chat", json={"message": "first"}).json()
        db_path.unlink()

        followup = client.post(
            "/api/chat",
            json={"message": "second", "conversation_id": first["conversation_id"]},
        )
        assert followup.status_code == 503
        assert followup.json() == {"detail": "conversation storage unavailable"}

        new_chat = client.post("/api/chat", json={"message": "new conversation"})
        assert new_chat.status_code == 503
        assert new_chat.json() == {"detail": "conversation storage unavailable"}

        stream = client.post(
            "/api/chat/stream",
            json={"message": "second", "conversation_id": first["conversation_id"]},
        )
        assert stream.status_code == 200
        assert 'event: error\ndata: {"message": "conversation storage unavailable"}' in stream.text
        assert "event: done" not in stream.text

        new_stream = client.post("/api/chat/stream", json={"message": "new conversation"})
        assert "event: delta" in new_stream.text
        assert (
            'event: error\ndata: {"message": "conversation storage unavailable"}'
            in new_stream.text
        )
        assert "event: done" not in new_stream.text
        assert not db_path.exists()


def test_prompt_window_is_bounded_while_full_transcript_remains(tmp_path: Path) -> None:
    database = Database(tmp_path / "bounded.sqlite3")
    database.initialize()
    provider = Provider()

    async def run() -> None:
        service = ChatService(provider, SQLiteConversationStore(database, max_messages=2))
        first = await service.complete("one")
        await service.complete("two", first.conversation_id)
        restarted = ChatService(provider, SQLiteConversationStore(database, max_messages=2))
        await restarted.complete("three", first.conversation_id)

    asyncio.run(run())
    assert [item.content for item in provider.requests[-1].messages[1:]] == [
        "two", "reply", "three"
    ]
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 6
