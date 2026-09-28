import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from backend.core.config import Settings
from backend.providers.base import Generation, LLMProvider, Message, ProviderError
from backend.providers.factory import create_provider
from backend.providers.openai import OpenAIProvider


class FakeProvider:
    def generate(self, messages: Sequence[Message]) -> Generation:
        return Generation("fake reply", "fake", "fixture")


def _response(text: str = "Hello", status: str = "completed") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": status,
            "output": [
                {"type": "reasoning"},
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
            ],
        },
    )


def test_openai_adapter_maps_messages_without_storing_response() -> None:
    observed: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        observed["request"] = request
        return _response()

    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = OpenAIProvider("test-key", "test-model", client=client)

    result = provider.generate([Message("system", "Be brief"), Message("user", "Hi")])

    assert result == Generation("Hello", "openai", "test-model")
    request = observed["request"]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": "test-model",
        "input": [
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "Hi"},
        ],
        "store": False,
    }


def test_provider_failure_does_not_expose_exception() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-key")

    client = httpx.Client(transport=httpx.MockTransport(fail))
    provider = OpenAIProvider("secret-key", "test-model", client=client)
    with pytest.raises(ProviderError, match="LLM request failed") as error:
        provider.generate([Message("user", "Hi")])
    assert "secret-key" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("response", "expected"),
    [(_response(""), "no text"), (_response(status="incomplete"), "not completed")],
)
def test_invalid_response_is_an_error(response: httpx.Response, expected: str) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    provider = OpenAIProvider("test-key", "test-model", client=client)
    with pytest.raises(ProviderError, match=expected):
        provider.generate([Message("user", "Hi")])


def test_malformed_content_is_an_error() -> None:
    response = httpx.Response(
        200,
        json={"status": "completed", "output": [{"type": "message", "content": None}]},
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: response))
    provider = OpenAIProvider("test-key", "test-model", client=client)
    with pytest.raises(ProviderError, match="no text"):
        provider.generate([Message("user", "Hi")])


def test_factory_requires_key_and_keeps_it_out_of_repr(tmp_path: Path) -> None:
    settings = Settings.from_env(
        tmp_path / "missing.env", {"OPENAI_API_KEY": "secret-key"}
    )
    assert "secret-key" not in repr(settings)
    provider = create_provider(settings)
    assert isinstance(provider, LLMProvider)
    without_key = Settings("test", "INFO", tmp_path, tmp_path / "db.sqlite3")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_provider(without_key)


def test_message_validation() -> None:
    with pytest.raises(ValueError, match="role"):
        Message("invalid", "Hi")
    with pytest.raises(ValueError, match="content"):
        Message("user", "   ")


def test_provider_contract_accepts_fake() -> None:
    provider: LLMProvider = FakeProvider()
    assert provider.generate([Message("user", "Hi")]).text == "fake reply"
