"""OpenAI adapter tests without network access or a real API key."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI

from backend.core.config import ConfigError
from backend.providers.base import ChatMessage, CompletionRequest, ProviderError
from backend.providers.openai import OpenAIResponsesProvider


class FakeResponses:
    def __init__(
        self,
        *,
        output: str = "Hello.",
        status: str = "completed",
        error: Exception | None = None,
        events: list[object] | None = None,
    ) -> None:
        self.output = output
        self.status = status
        self.error = error
        self.events = events
        self.calls: list[dict[str, object]] = []
        self.stream_closed = False

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if kwargs.get("stream"):
            return FakeStream(
                self.events
                if self.events is not None
                else [
                    SimpleNamespace(type="response.output_text.delta", delta="Hel"),
                    SimpleNamespace(type="response.output_text.delta", delta="lo."),
                    SimpleNamespace(type="response.completed"),
                ],
                self,
            )
        return SimpleNamespace(output_text=self.output, status=self.status)


class FakeStream:
    def __init__(self, events: list[object], responses: FakeResponses) -> None:
        self.events = events
        self.responses = responses

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.responses.stream_closed = True

    def __aiter__(self):
        async def iterate():
            for event in self.events:
                yield event

        return iterate()


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=(
            ChatMessage(role="system", content="Be concise."),
            ChatMessage(role="user", content="Greet me."),
        )
    )


def test_complete_maps_messages_and_disables_remote_storage() -> None:
    responses = FakeResponses()
    provider = OpenAIResponsesProvider(
        model="configured-model", client=SimpleNamespace(responses=responses)
    )

    result = asyncio.run(provider.complete(_request()))

    assert (result.text, result.provider, result.model) == ("Hello.", "openai", "configured-model")
    assert provider.name == "openai"
    assert responses.calls == [
        {
            "model": "configured-model",
            "input": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Greet me."},
            ],
            "store": False,
        }
    ]


def test_official_sdk_serializes_response_request() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-only"
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "configured-model",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Hello.", "annotations": []}
                        ],
                    }
                ],
            },
        )

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = AsyncOpenAI(api_key="test-only", http_client=http_client)
            provider = OpenAIResponsesProvider(model="configured-model", client=client)
            return (await provider.complete(_request())).text

    assert asyncio.run(run()) == "Hello."
    assert sent == [
        {
            "input": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Greet me."},
            ],
            "model": "configured-model",
            "store": False,
        }
    ]


def test_stream_yields_text_deltas() -> None:
    responses = FakeResponses()
    provider = OpenAIResponsesProvider(
        model="configured-model", client=SimpleNamespace(responses=responses)
    )

    async def collect() -> list[str]:
        return [delta async for delta in provider.stream(_request())]

    assert asyncio.run(collect()) == ["Hel", "lo."]
    assert responses.calls[0]["stream"] is True
    assert responses.calls[0]["store"] is False
    assert responses.stream_closed


def test_official_sdk_streams_text_deltas() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        events = [
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo."},
            {"type": "response.completed", "response": {"id": "resp_test"}},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async def collect() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = AsyncOpenAI(api_key="test-only", http_client=http_client)
            provider = OpenAIResponsesProvider(model="configured-model", client=client)
            return [delta async for delta in provider.stream(_request())]

    assert asyncio.run(collect()) == ["Hel", "lo."]
    assert sent[0]["stream"] is True
    assert sent[0]["store"] is False


def test_stream_rejects_incomplete_and_closes_stream() -> None:
    responses = FakeResponses(events=[SimpleNamespace(type="response.incomplete")])
    provider = OpenAIResponsesProvider(
        model="configured-model", client=SimpleNamespace(responses=responses)
    )

    async def collect() -> list[str]:
        return [delta async for delta in provider.stream(_request())]

    with pytest.raises(ProviderError, match="stream failed"):
        asyncio.run(collect())
    assert responses.stream_closed


def test_stream_rejects_completed_response_without_text() -> None:
    responses = FakeResponses(events=[SimpleNamespace(type="response.completed")])
    provider = OpenAIResponsesProvider(
        model="configured-model", client=SimpleNamespace(responses=responses)
    )

    async def collect() -> list[str]:
        return [delta async for delta in provider.stream(_request())]

    with pytest.raises(ProviderError, match="returned no text"):
        asyncio.run(collect())


def test_api_failure_has_safe_public_error() -> None:
    error = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    responses = FakeResponses(error=error)
    provider = OpenAIResponsesProvider(
        model="configured-model", client=SimpleNamespace(responses=responses)
    )

    with pytest.raises(ProviderError, match="^OpenAI request failed$"):
        asyncio.run(provider.complete(_request()))


def test_missing_credentials_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_OPENAI_MODEL", "configured-model")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        OpenAIResponsesProvider.from_env()


def test_empty_response_is_not_treated_as_success() -> None:
    provider = OpenAIResponsesProvider(
        model="configured-model", client=SimpleNamespace(responses=FakeResponses(output=""))
    )
    with pytest.raises(ProviderError, match="returned no text"):
        asyncio.run(provider.complete(_request()))


def test_incomplete_response_is_not_treated_as_success() -> None:
    provider = OpenAIResponsesProvider(
        model="configured-model",
        client=SimpleNamespace(responses=FakeResponses(output="Partial", status="incomplete")),
    )
    with pytest.raises(ProviderError, match="did not complete"):
        asyncio.run(provider.complete(_request()))
