"""Groq Chat Completions adapter contract with a fake HTTP client."""

import asyncio
import json

import httpx
import pytest

from backend.core.config import ConfigError, Settings, parse_model_choices
from backend.providers.base import ChatMessage, CompletionRequest, ProviderError
from backend.providers.choices import build_provider, provider_ready
from backend.providers.factory import create_provider
from backend.providers.groq import GroqProvider

MODEL = "vendor/model-x"


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=(
            ChatMessage("system", "Be concise."),
            ChatMessage("user", "Hello"),
            ChatMessage("assistant", "Hi"),
            ChatMessage("user", "Continue"),
        )
    )


def _full(text: str, reason: str | None = "stop") -> dict[str, object]:
    message = {"role": "assistant", "content": text}
    return {"choices": [{"message": message, "finish_reason": reason}]}


def _delta(text: str | None, reason: str | None = None) -> str:
    delta = {} if text is None else {"content": text}
    chunk = {"choices": [{"delta": delta, "finish_reason": reason}]}
    return f"data: {json.dumps(chunk)}\n\n"


def _run(handler, action) -> None:
    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await action(GroqProvider(model=MODEL, api_key="fake-key", client=client))

    asyncio.run(go())


def _sse(text: str):
    return lambda _r: httpx.Response(
        200, text=text, headers={"Content-Type": "text/event-stream"}
    )


def test_maps_contract_to_chat_completions_and_hides_key() -> None:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_full("Answer"))

    async def action(provider: GroqProvider) -> None:
        result = await provider.complete(_request())
        assert (result.text, result.provider, result.model) == ("Answer", "groq", MODEL)

    _run(reply, action)
    request = seen[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer fake-key"
    body = json.loads(request.content)
    assert body["model"] == MODEL and "stream" not in body
    assert body["messages"][0] == {"role": "system", "content": "Be concise."}
    assert body["messages"][-1] == {"role": "user", "content": "Continue"}


def test_streams_text_until_stop_and_done() -> None:
    seen: list[httpx.Request] = []
    sse = _delta("Hel") + _delta("lo") + _delta(None, "stop") + "data: [DONE]\n\n"

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _sse(sse)(request)

    async def action(provider: GroqProvider) -> None:
        assert [p async for p in provider.stream(_request())] == ["Hel", "lo"]

    _run(reply, action)
    assert json.loads(seen[0].content)["stream"] is True


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": "sentinel-private-token"}),
        (200, _full("Partial", "length")),
        (200, _full("", "stop")),
        (200, _full("   ", "stop")),
        (200, {"choices": []}),
        (200, ["not", "an", "object"]),
        (200, {"choices": [{"message": {"content": 5}, "finish_reason": "stop"}]}),
    ],
)
def test_complete_failures_are_sanitized(status: int, body: object) -> None:
    async def action(provider: GroqProvider) -> None:
        with pytest.raises(ProviderError) as failure:
            await provider.complete(_request())
        assert "sentinel-private-token" not in str(failure.value)
        assert failure.value.__cause__ is None

    _run(lambda _r: httpx.Response(status, json=body), action)


@pytest.mark.parametrize(
    ("sse", "message"),
    [
        (_delta("Partial", "length") + "data: [DONE]\n\n", "stream failed"),
        (_delta("Partial"), "before completion"),
        (_delta(None, "stop") + "data: [DONE]\n\n", "no text"),
        ('data: {"error": {"message": "sentinel-private-token"}}\n\n', "stream failed"),
        ("data: {not json\n\n", "request failed"),
    ],
)
def test_stream_failures_are_sanitized(sse: str, message: str) -> None:
    async def action(provider: GroqProvider) -> None:
        with pytest.raises(ProviderError, match=message) as failure:
            _ = [p async for p in provider.stream(_request())]
        assert "sentinel-private-token" not in str(failure.value)

    _run(_sse(sse), action)


def test_http_error_on_stream_is_sanitized() -> None:
    async def action(provider: GroqProvider) -> None:
        with pytest.raises(ProviderError, match="request failed"):
            _ = [p async for p in provider.stream(_request())]

    _run(lambda _r: httpx.Response(429, json={"error": "x"}), action)


def test_empty_request_and_bad_config() -> None:
    with pytest.raises(ProviderError):
        GroqProvider(model=MODEL, api_key="fake-key")._body(CompletionRequest(()), stream=False)
    with pytest.raises(ConfigError, match="JARVIS_GROQ_MODEL"):
        GroqProvider(model="bad model!", api_key="fake-key")
    with pytest.raises(ConfigError, match="JARVIS_GROQ_MODEL"):
        GroqProvider(model="", api_key="fake-key")
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        GroqProvider(model=MODEL, api_key=" ")


def test_factory_requires_key_and_explicit_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = Settings(db_path=tmp_path / "g.sqlite3", llm_provider="groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_GROQ_MODEL", MODEL)
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        create_provider(settings)
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    monkeypatch.delenv("JARVIS_GROQ_MODEL", raising=False)
    with pytest.raises(ConfigError, match="JARVIS_GROQ_MODEL"):
        create_provider(settings)
    monkeypatch.setenv("JARVIS_GROQ_MODEL", MODEL)
    assert create_provider(settings).model == MODEL


def test_model_choices_accept_groq_ids_with_slash() -> None:
    assert parse_model_choices(f"groq:{MODEL}, openai:a") == (f"groq:{MODEL}", "openai:a")
    for bad in ("openai:a/b", "gemini:a/b", "groq:/x", "groq:a b", "groq:"):
        with pytest.raises(ConfigError):
            parse_model_choices(bad)


def test_registry_helpers_know_groq() -> None:
    assert provider_ready("groq", {"GROQ_API_KEY": "fake-key"})
    assert not provider_ready("groq", {})
    built = build_provider("groq", MODEL, {"GROQ_API_KEY": "fake-key"})
    assert (built.name, built.model) == ("groq", MODEL)
