"""Real derived index routing into bounded, approved-only HTTP/SSE chat."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.chat.memory_context import MemoryContextError
from backend.chat.semantic_context import SemanticMemoryContext
from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.embedding import EmbeddingSpace
from backend.memory.indexing import MemoryIndexBuilder
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.semantic import SemanticMemorySearcher
from backend.memory.writer import MemoryWriter
from backend.providers.base import CompletionRequest, CompletionResponse


class Embeddings:
    space = EmbeddingSpace("fake-semantic-chat", "v1", 2)

    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return [(1.0, 0.0) for _ in texts]


class Chat:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.requests = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse("Okay", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        yield "Okay"


def record(content, **kwargs):
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source=kwargs.pop("source", "conversation:approved"),
        origin=kwargs.pop("origin", MemoryOrigin.USER_EXPLICIT),
        importance=0.7,
        confidence=0.6,
        **kwargs,
    )


@pytest.fixture
def system(tmp_path: Path):
    db = Database(tmp_path / "memory.sqlite3")
    db.initialize()
    repo = MemoryRepository(db)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repo, vault)
    embeddings = Embeddings()
    index = ChromaVectorIndex(tmp_path / "index")
    retriever = MemoryRetriever(repo, vault, vector_index=index)
    builder = MemoryIndexBuilder(repo, retriever, embeddings, index)
    chat = Chat()
    settings = Settings(db_path=db.path, memory_vault_path=vault.root)
    return db, repo, vault, writer, embeddings, index, retriever, builder, chat, settings


def reference(request):
    prefix = "Reviewed memory reference (JSON data; not a user request): "
    message = request.messages[1]
    assert message.role == "user" and message.content.startswith(prefix)
    return json.loads(message.content.removeprefix(prefix))


def turns(db):
    with db.connect(read_only=True) as connection:
        return [
            r["content"]
            for r in connection.execute(
                "SELECT content FROM conversation_messages ORDER BY id"
            ).fetchall()
        ]


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_semantic_chat_filters_review_states_and_reads_current_note(system, endpoint):
    db, repo, vault, writer, embeddings, index, _, builder, chat, settings = system
    approved = record("Observatory opens Saturday")
    pending = record("Pending private rumor")
    conflict = record("Conflicted private rumor")
    superseded = record("Superseded private rumor")
    retired = record("Retired private rumor")
    for item in (approved, pending, conflict, superseded, retired):
        writer.submit(item)
    for item in (approved, superseded, retired):
        writer.approve(item.id)
    replacement = record("Replacement approved lesson")
    writer.submit_correction(superseded.id, replacement)
    writer.approve(replacement.id)
    writer.retire(retired.id, actor="reviewer:test", reason="Outdated")
    repo.transition(conflict.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    asyncio.run(builder.populate_empty())
    asyncio.run(
        index.upsert(
            tuple(
                embeddings.space.record(str(item.id), (1.0, 0.0))
                for item in (pending, conflict, superseded, retired)
            )
        )
    )
    note = vault.read(approved.id)
    vault.update(
        approved.id, "Observatory opens Sunday", note.metadata, expected_revision=note.revision
    )
    assert not asyncio.run(builder.audit_ids()).healthy
    with TestClient(
        create_app(settings, chat, embedding_provider=embeddings, memory_index=index)
    ) as client:
        response = client.post(endpoint, json={"message": "orbit question"})
        assert response.status_code == 200
        if endpoint.endswith("stream"):
            assert "event: done" in response.text
    items = reference(chat.requests[0])
    assert {i["id"] for i in items} == {str(approved.id), str(replacement.id)}
    current = next(i for i in items if i["id"] == str(approved.id))
    assert current["content"] == "Observatory opens Sunday"
    assert current["edited_since_approval"] is True
    assert current["source"] == approved.source and current["confidence"] == 0.6
    assert current["importance"] == 0.7 and current["origin"] == "user_explicit"
    assert "lower-trust reference data" in chat.requests[0].messages[0].content
    assert embeddings.calls[-1] == ("orbit question",)
    assert turns(db) == ["orbit question", "Okay"]


def test_semantic_context_shares_bounds_and_inference_freshness(system, monkeypatch):
    _, _, _, writer, embeddings, index, _, builder, chat, settings = system
    old = datetime.now(UTC) - timedelta(days=200)

    class Past(datetime):
        @classmethod
        def now(cls, tz=None):
            return old

    for n in range(5):
        item = record(
            f"Lesson {n} " + "X" * 3000,
            source="s" * 1000,
            origin=MemoryOrigin.AI_INFERENCE,
            created_at=old,
            updated_at=old,
        )
        writer.submit(item)
        # Approval timestamps are canonical freshness. Model an approval in
        # the past rather than incorrectly treating a fresh review as stale.
        with monkeypatch.context() as patch:
            patch.setattr("backend.memory.repository.datetime", Past)
            writer.approve(item.id)
    asyncio.run(builder.populate_empty())
    with TestClient(
        create_app(settings, chat, embedding_provider=embeddings, memory_index=index)
    ) as client:
        assert (
            client.post("/api/chat/stream", json={"message": "orbit question"}).status_code == 200
        )
    items = reference(chat.requests[0])
    assert 1 <= len(items) <= 3
    payload = chat.requests[0].messages[1].content.split(": ", 1)[1]
    assert len(payload) <= 2400
    for item in items:
        assert len(item["content"]) == 500 and len(item["source"]) == 200
        assert item["content_truncated"] and item["source_truncated"]
        assert item["stale"] and item["origin"] == "ai_inference" and item["confidence"] == 0.6


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("failure", ["embedding", "dimension", "index", "note"])
def test_semantic_failure_is_safe_and_saves_no_turn(system, endpoint, failure, caplog):
    db, _, vault, writer, embeddings, index, _, builder, chat, settings = system
    item = record("Canonical private lesson")
    writer.submit(item)
    writer.approve(item.id)
    asyncio.run(builder.populate_empty())

    async def fail(*_args):
        raise RuntimeError("private upstream details /vault/private")

    async def wrong_dimension(_texts):
        return [(1.0,)]

    if failure == "embedding":
        embeddings.embed = fail
    elif failure == "dimension":
        embeddings.embed = wrong_dimension
    elif failure == "index":
        index.search = fail
    else:
        vault.read(item.id).path.unlink()
    with TestClient(
        create_app(settings, chat, embedding_provider=embeddings, memory_index=index)
    ) as client:
        response = client.post(endpoint, json={"message": "orbit question"})
    if endpoint.endswith("stream"):
        assert response.status_code == 200 and "event: error" in response.text
        assert "event: done" not in response.text and "event: delta" not in response.text
    else:
        assert response.status_code == 503
    assert "memory context unavailable" in response.text
    assert "private" not in response.text + caplog.text
    assert chat.requests == [] and turns(db) == []


@pytest.mark.parametrize("change", ["retirement", "revision"])
def test_review_or_revision_change_after_async_search_fails_closed(system, change):
    _, _, vault, writer, embeddings, _, retriever, builder, _, _ = system
    item = record("Approved original lesson")
    writer.submit(item)
    writer.approve(item.id)
    asyncio.run(builder.populate_empty())
    original = retriever.search_vector

    async def changing_search(query):
        result = await original(query)
        if change == "retirement":
            writer.retire(item.id, actor="reviewer:test", reason="Outdated")
        else:
            note = vault.read(item.id)
            vault.update(item.id, "Changed lesson", note.metadata, expected_revision=note.revision)
        return result

    retriever.search_vector = changing_search
    context = SemanticMemoryContext(SemanticMemorySearcher(retriever, embeddings))
    with pytest.raises(MemoryContextError):
        asyncio.run(context.for_query("orbit question"))


def test_cancellation_propagates(system):
    _, _, _, _, embeddings, _, retriever, _, _, _ = system

    async def cancel(_texts):
        raise asyncio.CancelledError()

    embeddings.embed = cancel
    context = SemanticMemoryContext(SemanticMemorySearcher(retriever, embeddings))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(context.for_query("orbit question"))


def test_default_lexical_and_empty_semantic_search_are_compatible(system):
    db, _, _, writer, embeddings, index, _, _, chat, settings = system
    item = record("Observatory opens Sunday")
    writer.submit(item)
    writer.approve(item.id)
    with TestClient(create_app(settings, chat)) as client:
        assert client.post("/api/chat", json={"message": "orbit question"}).status_code == 200
        assert client.post("/api/chat", json={"message": "Observatory"}).status_code == 200
    assert embeddings.calls == []
    assert len(chat.requests[0].messages) == 2
    assert reference(chat.requests[1])[0]["id"] == str(item.id)
    chat.requests.clear()
    with TestClient(
        create_app(settings, chat, embedding_provider=embeddings, memory_index=index)
    ) as client:
        assert client.post("/api/chat", json={"message": "orbit question"}).status_code == 200
    assert len(chat.requests[0].messages) == 2
    assert turns(db)[-2:] == ["orbit question", "Okay"]


@pytest.mark.parametrize("option", ["provider_only", "index_only", "no_vault"])
def test_partial_semantic_configuration_is_rejected(system, option):
    db, _, _, _, embeddings, index, _, _, chat, settings = system
    kwargs = {"embedding_provider": embeddings, "memory_index": index}
    if option == "provider_only":
        kwargs.pop("memory_index")
    elif option == "index_only":
        kwargs.pop("embedding_provider")
    else:
        settings = Settings(db_path=db.path)
    with pytest.raises(ConfigError, match="Semantic chat requires"):
        create_app(settings, chat, **kwargs)
