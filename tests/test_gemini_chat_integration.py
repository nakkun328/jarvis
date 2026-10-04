"""Gemini adapter through chat, SSE, reviewed memory, and durable context."""

import json
import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings
from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.writer import MemoryWriter
from backend.providers.gemini import GeminiProvider


def _model_reply(text: str, *, finished: bool = True) -> dict[str, object]:
    candidate: dict[str, object] = {"content": {"parts": [{"text": text}]}}
    if finished:
        candidate["finishReason"] = "STOP"
    return {"candidates": [candidate]}


def _fake_client(seen: list[dict[str, object]]) -> httpx.AsyncClient:
    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path.endswith(":streamGenerateContent"):
            sse = "".join(
                f"data: {json.dumps(chunk)}\n\n"
                for chunk in (
                    _model_reply("Ack", finished=False),
                    _model_reply("nowledged"),
                )
            )
            return httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=_model_reply("Acknowledged"))

    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


def _memory(content: str) -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source="conversation:gemini-smoke",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.7,
        confidence=1.0,
    )


def test_chat_sse_followup_memory_and_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "chat.sqlite3")
    database.initialize()
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(MemoryRepository(database), vault)
    approved = _memory("Observatory opens on Saturday")
    pending = _memory("Observatory is permanently closed")
    writer.submit(approved)
    writer.submit(pending)
    writer.approve(approved.id)

    seen: list[dict[str, object]] = []
    settings = Settings(db_path=database.path, memory_vault_path=vault.root)
    first_client = _fake_client(seen)
    provider = GeminiProvider(model="gemini-2.5-flash", api_key="test-key", client=first_client)
    with TestClient(create_app(settings, provider)) as client:
        first = client.post("/api/chat", json={"message": "When does Observatory open?"})
        assert first.status_code == 200
        first_body = first.json()
        conversation_id = first_body["conversation_id"]
        assert first_body["provider"] == "gemini"
        assert first_body["model"] == "gemini-2.5-flash"

        follow = client.post(
            "/api/chat/stream",
            json={"message": "Continue", "conversation_id": conversation_id},
        )
        assert follow.status_code == 200
        assert follow.headers["content-type"].startswith("text/event-stream")
        assert follow.text.count("event: delta") == 2
        assert 'event: done\ndata: ' in follow.text
        assert conversation_id in follow.text
        assert '"provider": "gemini"' in follow.text

    assert first_client.is_closed

    first_request = json.dumps(seen[0])
    assert approved.content in first_request
    assert pending.content not in first_request
    assert seen[0]["store"] is False
    assert "Reviewed memory reference" in first_request
    assert "Reviewed memory reference" not in json.dumps(seen[1])
    assert [entry["role"] for entry in seen[1]["contents"]] == ["user", "model", "user"]

    second_client = _fake_client(seen)
    restarted_provider = GeminiProvider(
        model="gemini-2.5-flash", api_key="test-key", client=second_client
    )
    with TestClient(create_app(settings, restarted_provider)) as client:
        resumed = client.post(
            "/api/chat", json={"message": "After restart", "conversation_id": conversation_id}
        )
        assert resumed.status_code == 200
        assert resumed.json()["conversation_id"] == conversation_id

    assert second_client.is_closed

    assert [entry["role"] for entry in seen[2]["contents"]] == [
        "user", "model", "user", "model", "user"
    ]
    with sqlite3.connect(database.path) as connection:
        rows = connection.execute(
            "SELECT role, content FROM conversation_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    assert len(rows) == 6
    assert [row[0] for row in rows] == ["user", "assistant"] * 3


def test_api_and_stream_errors_hide_gemini_details(tmp_path: Path, caplog) -> None:
    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "sentinel-private-token"})

    fake_client = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    provider = GeminiProvider(model="gemini-2.5-flash", api_key="test-key", client=fake_client)
    with TestClient(create_app(Settings(db_path=tmp_path / "chat.sqlite3"), provider)) as client:
        regular = client.post("/api/chat", json={"message": "Hello"})
        stream = client.post("/api/chat/stream", json={"message": "Hello"})
    assert regular.status_code == 502
    assert regular.json()["detail"] == "chat provider failed"
    assert stream.status_code == 200
    assert 'event: error\ndata: {"message": "chat provider failed"}' in stream.text
    assert "sentinel-private-token" not in regular.text + stream.text + caplog.text
    assert fake_client.is_closed
