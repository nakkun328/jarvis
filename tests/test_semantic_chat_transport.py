"""HTTP/SSE cancellation and retry with semantic memory and durable history."""

import asyncio
import json

import httpx
import pytest
import test_semantic_chat as memory_fixtures
from starlette.requests import ClientDisconnect
from test_semantic_chat import Chat, record, reference, turns

from backend.api.app import create_app
from backend.chat.service import ChatService
from backend.memory.embedding import EmbeddingSpace

system = memory_fixtures.system


class ClosingChat(Chat):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.stream_closed = False
        self.iterator = None

    async def aclose(self):
        self.closed = True


def configured(system, monkeypatch):
    db, _, vault, writer, embeddings, index, _, builder, _, settings = system
    item = record("Reviewed lesson")
    writer.submit(item)
    writer.approve(item.id)
    asyncio.run(builder.populate_empty())
    services = []

    def capture(*args, **kwargs):
        service = ChatService(*args, **kwargs)
        services.append(service)
        return service

    monkeypatch.setattr("backend.api.app.ChatService", capture)
    chat = ClosingChat()
    app = create_app(settings, chat, embedding_provider=embeddings, memory_index=index)
    return db, vault, item, embeddings, chat, app, services[0]


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_http_cancel_during_embedding_preserves_history_and_allows_retry(
    system, monkeypatch, endpoint
):
    db, _, _, embeddings, chat, app, service = configured(system, monkeypatch)

    async def run():
        entered = asyncio.Event()
        blocked = asyncio.Event()
        released = False
        original = embeddings.embed

        async def wait_for_embedding(_texts):
            nonlocal released
            try:
                entered.set()
                await blocked.wait()
            finally:
                released = True

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                first = await client.post("/api/chat", json={"message": "initial"})
                cid = first.json()["conversation_id"]
                embeddings.embed = wait_for_embedding
                task = asyncio.create_task(
                    client.post(endpoint, json={"message": "cancelled", "conversation_id": cid})
                )
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert released and len(chat.requests) == 1
                assert turns(db) == ["initial", "Okay"]
                conversation = next(iter(service.store._conversations.values()))
                assert conversation.active_requests == 0 and not conversation.lock.locked()
                embeddings.embed = original
                retry = await client.post(
                    endpoint, json={"message": "retry", "conversation_id": cid}
                )
                assert retry.status_code == 200
                if endpoint.endswith("stream"):
                    assert "event: done" in retry.text
                assert turns(db) == ["initial", "Okay", "retry", "Okay"]
                assert "cancelled" not in repr(chat.requests[-1])
        assert chat.closed

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["send-failure", "disconnect", "cancel-send"])
def test_real_sse_transport_interruption_closes_semantic_stream_and_session(
    system, monkeypatch, interruption
):
    db, _, _, _, chat, app, service = configured(system, monkeypatch)

    async def run():
        sending = asyncio.Event()
        blocked = asyncio.Event()
        body_sent = False
        payload = json.dumps({"message": "interrupted"}).encode()

        def stream(request):
            async def generate():
                try:
                    chat.requests.append(request)
                    yield "partial"
                    await blocked.wait()
                finally:
                    chat.stream_closed = True

            chat.iterator = generate()
            return chat.iterator

        chat.stream = stream

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await sending.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                sending.set()
                if interruption == "send-failure":
                    raise OSError("fixture transport unavailable")
                await blocked.wait()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/chat/stream",
            "raw_path": b"/api/chat/stream",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "scheme": "http",
            "http_version": "1.1",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
            "asgi": {"spec_version": "2.0" if interruption == "disconnect" else "2.4"},
        }
        async with app.router.lifespan_context(app):
            if interruption == "send-failure":
                with pytest.raises(ClientDisconnect):
                    await app(scope, receive, send)
            elif interruption == "disconnect":
                await asyncio.wait_for(app(scope, receive, send), timeout=5)
            else:
                task = asyncio.create_task(app(scope, receive, send))
                await asyncio.wait_for(sending.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert chat.iterator is not None and chat.stream_closed
            assert reference(chat.requests[0])[0]["content"] == "Reviewed lesson"
            assert turns(db) == [] and not service.store._conversations
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                assert (
                    await client.post("/api/chat", json={"message": "retry"})
                ).status_code == 200
            assert turns(db) == ["retry", "Okay"]
        assert chat.closed

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("failure", ["embedding", "contract"])
def test_transient_semantic_failure_retries_same_conversation_with_current_note(
    system, monkeypatch, endpoint, failure, caplog
):
    db, vault, item, embeddings, chat, app, service = configured(system, monkeypatch)

    async def run():
        original = embeddings.embed
        original_space = embeddings.space

        async def fail(texts):
            if failure == "contract":
                values = await original(texts)
                embeddings.space = EmbeddingSpace(
                    original_space.name, "fixture-private-v2", original_space.dimension
                )
                return values
            raise RuntimeError("fixture-private-upstream-details")

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                first = await client.post("/api/chat", json={"message": "initial"})
                cid = first.json()["conversation_id"]
                embeddings.embed = fail
                failed = await client.post(
                    endpoint, json={"message": "failed", "conversation_id": cid}
                )
                assert "memory context unavailable" in failed.text
                assert "fixture-private" not in failed.text + caplog.text
                assert len(chat.requests) == 1 and turns(db) == ["initial", "Okay"]
                conversation = next(iter(service.store._conversations.values()))
                assert conversation.active_requests == 0 and not conversation.lock.locked()
                note = vault.read(item.id)
                vault.update(
                    item.id,
                    "Current reviewed lesson",
                    note.metadata,
                    expected_revision=note.revision,
                )
                embeddings.embed = original
                embeddings.space = original_space
                retried = await client.post(
                    endpoint, json={"message": "retry", "conversation_id": cid}
                )
                assert retried.status_code == 200
                if endpoint.endswith("stream"):
                    assert "event: done" in retried.text and "event: error" not in retried.text
                assert turns(db) == ["initial", "Okay", "retry", "Okay"]
                assert reference(chat.requests[-1])[0]["content"] == "Current reviewed lesson"
                assert "failed" not in repr(chat.requests[-1])
        assert chat.closed

    asyncio.run(run())
