"""Chat API and context behavior with an injected provider."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.chat.context import (
    ConversationCapacityError,
    ConversationNotFound,
    ConversationStore,
)
from backend.chat.service import ChatService
from backend.core.config import ConfigError, Settings
from backend.providers.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    ProviderError,
)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.fail = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.fail:
            raise ProviderError("private provider details")
        return CompletionResponse("Acknowledged.", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        if self.fail:
            raise ProviderError("private provider details")
        yield "Acknow"
        yield "ledged."


def test_chat_api_reuses_bounded_conversation_context(tmp_path: Path) -> None:
    provider = FakeProvider()
    with TestClient(create_app(Settings(db_path=tmp_path / "chat.sqlite3"), provider)) as client:
        first = client.post("/api/chat", json={"message": "Hello"})
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["reply"] == "Acknowledged."
        assert first_body["provider"] == "fake"
        assert first_body["model"] == "fake-model"

        second = client.post(
            "/api/chat",
            json={"message": "Continue", "conversation_id": first_body["conversation_id"]},
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == first_body["conversation_id"]

    assert [message.role for message in provider.requests[0].messages] == ["system", "user"]
    assert [message.role for message in provider.requests[1].messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert provider.requests[1].messages[-2:] == (
        ChatMessage(role="assistant", content="Acknowledged."),
        ChatMessage(role="user", content="Continue"),
    )


def test_stream_contract_and_followup_context(tmp_path: Path) -> None:
    provider = FakeProvider()
    with TestClient(create_app(Settings(db_path=tmp_path / "stream.sqlite3"), provider)) as client:
        stream = client.post("/api/chat/stream", json={"message": "Hello"})
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert 'event: delta\ndata: {"text": "Acknow"}\n\n' in stream.text
        assert 'event: delta\ndata: {"text": "ledged."}\n\n' in stream.text
        assert "event: done\ndata: " in stream.text

        done_data = json.loads(stream.text.split("event: done\ndata: ", 1)[1].split("\n\n", 1)[0])
        assert done_data["provider"] == "fake"
        assert done_data["model"] == "fake-model"
        followup = client.post(
            "/api/chat",
            json={"message": "Continue", "conversation_id": done_data["conversation_id"]},
        )
        assert followup.status_code == 200

    assert provider.requests[-1].messages[-2].content == "Acknowledged."


def test_invalid_input_and_missing_provider_are_safe(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(db_path=tmp_path / "none.sqlite3"))) as client:
        assert client.post("/api/chat", json={"message": "Hello"}).status_code == 503
        assert client.post("/api/chat/stream", json={"message": "Hello"}).status_code == 503
        assert client.post("/api/chat", json={"message": "   "}).status_code == 422
        assert (
            client.post(
                "/api/chat", json={"message": "Hello", "conversation_id": "invalid"}
            ).status_code
            == 422
        )

    provider = FakeProvider()
    with TestClient(create_app(Settings(db_path=tmp_path / "failure.sqlite3"), provider)) as client:
        provider.fail = True
        response = client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 502
        assert "private" not in response.text
        stream = client.post("/api/chat/stream", json={"message": "Hello"})
        assert "event: error" in stream.text
        assert "private" not in stream.text


def test_context_trims_turns_and_skips_failed_responses() -> None:
    provider = FakeProvider()
    service = ChatService(provider, ConversationStore(max_messages=2))

    async def run() -> None:
        first = await service.complete("one")
        provider.fail = True
        try:
            await service.complete("failed", first.conversation_id)
        except ProviderError:
            pass
        else:
            raise AssertionError("provider failure must propagate")
        provider.fail = False
        await service.complete("two", first.conversation_id)
        await service.complete("three", first.conversation_id)

    asyncio.run(run())
    assert [message.content for message in provider.requests[-1].messages[1:]] == [
        "two",
        "Acknowledged.",
        "three",
    ]


def test_rejects_unknown_provider_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "unsupported")
    with pytest.raises(ConfigError, match="JARVIS_LLM_PROVIDER"):
        Settings.from_env()


def test_conversation_capacity_evicts_only_idle_sessions() -> None:
    store = ConversationStore(max_conversations=1)

    async def run() -> None:
        async with store.open(None) as (first_id, _):
            with pytest.raises(ConversationCapacityError):
                async with store.open(None):
                    pass
        async with store.open(None):
            pass
        with pytest.raises(ConversationNotFound):
            async with store.open(first_id):
                pass

    asyncio.run(run())
