"""Gemini REST adapter contract with a fake HTTP client."""

import asyncio
import json

import httpx
import pytest

from backend.core.config import ConfigError, Settings
from backend.providers.base import ChatMessage, CompletionRequest, ProviderError
from backend.providers.factory import create_provider
from backend.providers.gemini import GeminiProvider


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=(
            ChatMessage("system", "Be concise."),
            ChatMessage("user", "Reviewed memory reference: approved note"),
            ChatMessage("user", "Hello"),
            ChatMessage("assistant", "Hi"),
            ChatMessage("user", "Continue"),
        )
    )


def _chunk(text: str, reason: str | None = None) -> dict[str, object]:
    candidate: dict[str, object] = {"content": {"parts": [{"text": text}]}}
    if reason is not None:
        candidate["finishReason"] = reason
    return {"candidates": [candidate]}


def test_maps_provider_contract_to_official_request_and_hides_key() -> None:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chunk("Answer", "STOP"))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            provider = GeminiProvider(model="gemini-2.5-flash", api_key="test-key", client=client)
            result = await provider.complete(_request())
            assert (result.text, result.provider, result.model) == (
                "Answer", "gemini", "gemini-2.5-flash"
            )

    asyncio.run(run())
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url).endswith("/models/gemini-2.5-flash:generateContent")
    assert "test-key" not in str(request.url)
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    assert body["store"] is False
    assert body["systemInstruction"] == {"parts": [{"text": "Be concise."}]}
    assert body["contents"] == [
        {
            "role": "user",
            "parts": [
                {"text": "Reviewed memory reference: approved note"},
                {"text": "Hello"},
            ],
        },
        {"role": "model", "parts": [{"text": "Hi"}]},
        {"role": "user", "parts": [{"text": "Continue"}]},
    ]


def test_streams_text_and_requires_successful_finish() -> None:
    seen: list[httpx.Request] = []
    sse = "".join(
        f"data: {json.dumps(chunk)}\n\n"
        for chunk in (_chunk("Hel"), _chunk("lo"), _chunk("", "STOP"))
    )

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            provider = GeminiProvider(model="gemini-2.5-flash", api_key="test-key", client=client)
            assert [part async for part in provider.stream(_request())] == ["Hel", "lo"]

    asyncio.run(run())
    assert str(seen[0].url).endswith("/models/gemini-2.5-flash:streamGenerateContent?alt=sse")
    assert json.loads(seen[0].content)["store"] is False


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": "sentinel-private-token"}),
        (200, _chunk("Partial", "MAX_TOKENS")),
        (200, _chunk("", "STOP")),
    ],
)
def test_complete_fails_without_exposing_provider_details(status: int, body: object) -> None:
    def reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            provider = GeminiProvider(model="gemini-2.5-flash", api_key="test-key", client=client)
            with pytest.raises(ProviderError) as failure:
                await provider.complete(_request())
            assert "sentinel-private-token" not in str(failure.value)

    asyncio.run(run())


def test_stream_failure_is_sanitized() -> None:
    sse = f"data: {json.dumps(_chunk('Partial', 'MAX_TOKENS'))}\n\n"

    def reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            provider = GeminiProvider(model="gemini-2.5-flash", api_key="test-key", client=client)
            with pytest.raises(ProviderError, match="stream failed"):
                _ = [part async for part in provider.stream(_request())]

    asyncio.run(run())


def test_factory_requires_server_side_key_and_explicit_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = Settings(db_path=tmp_path / "chat.sqlite3", llm_provider="gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_GEMINI_MODEL", "gemini-2.5-flash")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        create_provider(settings)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("JARVIS_GEMINI_MODEL", raising=False)
    with pytest.raises(ConfigError, match="JARVIS_GEMINI_MODEL"):
        create_provider(settings)
    monkeypatch.setenv("JARVIS_GEMINI_MODEL", "gemini-2.5-flash")
    assert isinstance(create_provider(settings), GeminiProvider)
