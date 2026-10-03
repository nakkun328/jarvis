"""Provider and conversation resources close without waiting for generator GC."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from backend.api.app import create_app
from backend.api.chat import ChatRequest, build_chat_router
from backend.chat.context import ConversationStore
from backend.chat.service import ChatService
from backend.core.config import Settings
from backend.core.database import Database
from backend.providers.openai import OpenAIResponsesProvider


class ClosingProvider:
    name = "fake"
    model = "fake"

    def __init__(self):
        self.stream_closed = False
        self.client_closed = False
        self.iterator = None

    def stream(self, request):
        async def generate():
            try:
                yield "partial"
                yield "rest"
            finally:
                self.stream_closed = True

        # Keep a reference: deterministic cleanup must not depend on GC.
        self.iterator = generate()
        return self.iterator

    async def aclose(self):
        self.client_closed = True


@pytest.mark.parametrize("through_api", [False, True])
def test_closing_partial_stream_immediately_releases_provider_and_session(through_api):
    async def run():
        provider = ClosingProvider()
        store = ConversationStore(max_conversations=1)
        service = ChatService(provider, store)
        if through_api:
            endpoint = next(
                route.endpoint for route in build_chat_router(service).routes
                if route.path == "/api/chat/stream"
            )
            response = await endpoint(ChatRequest(message="hello"))
            stream = response.body_iterator
        else:
            stream = service.stream("hello")
        await anext(stream)
        conversation = next(iter(store._conversations.values()))
        assert conversation.active_requests == 1
        await stream.aclose()
        assert provider.stream_closed
        assert conversation.active_requests == 0
        assert not conversation.lock.locked()
        assert conversation.messages == []
        # The released capacity can immediately accept another request.
        async with store.open(None):
            pass

    asyncio.run(run())


def test_startup_failure_closes_provider(tmp_path, monkeypatch):
    provider = ClosingProvider()
    app = create_app(Settings(db_path=tmp_path / "chat.sqlite3"), provider)

    def fail_initialize(self):
        raise OSError("synthetic DB failure")

    monkeypatch.setattr(Database, "initialize", fail_initialize)
    with pytest.raises(OSError, match="synthetic DB failure"):
        with TestClient(app):
            pass
    assert provider.client_closed


def test_openai_client_closes_on_app_shutdown(tmp_path):
    client = AsyncOpenAI(
        api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        ))
    )
    provider = OpenAIResponsesProvider(model="configured-model", client=client)
    with TestClient(create_app(Settings(db_path=tmp_path / "chat.sqlite3"), provider)):
        assert not client.is_closed()
    assert client.is_closed()


def test_invalid_vault_is_checked_before_allocating_provider(tmp_path, monkeypatch):
    from backend.core.config import ConfigError

    created = []
    monkeypatch.setattr(
        "backend.api.app.create_provider", lambda settings: created.append(settings)
    )
    with pytest.raises(ConfigError, match="existing vault directory"):
        create_app(Settings(
            db_path=tmp_path / "chat.sqlite3", memory_vault_path=tmp_path / "missing"
        ))
    assert created == []


def test_provider_contract_accepts_async_iterator_without_close():
    class PlainIterator:
        def __init__(self):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return "complete"

    class Provider:
        name = "custom"
        model = "iterator"

        def stream(self, request):
            return PlainIterator()

    async def run():
        from backend.chat.service import ChatDelta, ChatDone

        store = ConversationStore()
        results = [item async for item in ChatService(Provider(), store).stream("hello")]
        assert results[0] == ChatDelta("complete")
        assert isinstance(results[1], ChatDone)
        conversation = store._conversations[results[1].conversation_id]
        assert [m.content for m in conversation.messages] == ["hello", "complete"]
        assert conversation.active_requests == 0

    asyncio.run(run())


@pytest.mark.parametrize("through_api", [False, True])
def test_cancel_during_provider_wait_releases_stream_and_does_not_save(tmp_path, through_api):
    from backend.chat.persistence import SQLiteConversationStore

    async def run():
        database = Database(tmp_path / "cancel.sqlite3")
        database.initialize()
        entered = asyncio.Event()
        blocked = asyncio.Event()

        class BlockingProvider(ClosingProvider):
            def stream(self, request):
                async def generate():
                    try:
                        yield "partial"
                        entered.set()
                        await blocked.wait()
                    finally:
                        self.stream_closed = True
                self.iterator = generate()
                return self.iterator

        provider = BlockingProvider()
        store = SQLiteConversationStore(database, max_conversations=1)
        service = ChatService(provider, store)
        if through_api:
            endpoint = next(route.endpoint for route in build_chat_router(service).routes
                            if route.path == "/api/chat/stream")
            response = await endpoint(ChatRequest(message="cancelled"))
            stream = response.body_iterator
        else:
            stream = service.stream("cancelled")
        await anext(stream)
        conversation = next(iter(store._conversations.values()))
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.stream_closed
        assert conversation.active_requests == 0
        assert not conversation.lock.locked()
        assert not store._conversations
        with database.connect(read_only=True) as connection:
            count = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
        assert count == 0
        async with store.open(None):
            pass  # Capacity is immediately available without GC or a delay.

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["send-failure", "disconnect", "cancel-send"])
def test_asgi_interruption_during_send_closes_held_body_and_sqlite_session(tmp_path, interruption):
    from starlette.requests import ClientDisconnect

    from backend.chat.persistence import SQLiteConversationStore

    async def run():
        database = Database(tmp_path / "send.sqlite3")
        database.initialize()
        provider = ClosingProvider()
        store = SQLiteConversationStore(database, max_conversations=1)
        service = ChatService(provider, store)
        endpoint = next(route.endpoint for route in build_chat_router(service).routes
                        if route.path == "/api/chat/stream")
        response = await endpoint(ChatRequest(message="interrupted"))
        sending = asyncio.Event()
        blocked = asyncio.Event()
        conversations = []

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                conversations.append(next(iter(store._conversations.values())))
                sending.set()
                if interruption == "send-failure":
                    raise OSError("disconnected transport")
                await blocked.wait()

        async def receive():
            await sending.wait()
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": (
            "2.0" if interruption == "disconnect" else "2.4"
        )}}
        if interruption == "send-failure":
            with pytest.raises(ClientDisconnect):
                await response(scope, receive, send)
        elif interruption == "disconnect":
            await asyncio.wait_for(response(scope, receive, send), timeout=5)
        else:
            task = asyncio.create_task(response(scope, receive, send))
            await asyncio.wait_for(sending.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Hold response/provider iterator references so GC cannot conceal a leak.
        assert provider.iterator is not None and response.body_iterator is not None
        assert provider.stream_closed
        assert conversations[0].active_requests == 0
        assert not conversations[0].lock.locked()
        assert not store._conversations
        with database.connect(read_only=True) as connection:
            count = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
        assert count == 0
        async with store.open(None):
            pass

    asyncio.run(run())
