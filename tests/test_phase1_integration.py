"""Cross-component Phase 1 smoke test with a simulated provider transport."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.core.config import Settings
from backend.providers.openai import OpenAIResponsesProvider


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return FakeStream()
        return SimpleNamespace(status="completed", output_text="Again")


class FakeStream:
    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def __aiter__(self):
        yield SimpleNamespace(type="response.output_text.delta", delta="Hello")
        yield SimpleNamespace(type="response.completed")


def test_static_ui_stream_and_followup_share_one_conversation(tmp_path: Path) -> None:
    responses = FakeResponses()
    provider = OpenAIResponsesProvider(
        model="test-model", client=SimpleNamespace(responses=responses)
    )
    with TestClient(create_app(Settings(db_path=tmp_path / "jarvis.sqlite3"), provider)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "/static/app.js" in page.text
        assert client.get("/static/app.js").status_code == 200

        stream = client.post("/api/chat/stream", json={"message": "First"})
        assert stream.status_code == 200
        assert 'event: delta\ndata: {"text": "Hello"}' in stream.text
        done = json.loads(stream.text.split("event: done\ndata: ", 1)[1].split("\n\n", 1)[0])
        assert done["provider"] == "openai"
        assert done["model"] == "test-model"

        followup = client.post(
            "/api/chat", json={"message": "Second", "conversation_id": done["conversation_id"]}
        )
        assert followup.status_code == 200
        assert followup.json()["reply"] == "Again"
        assert followup.json()["conversation_id"] == done["conversation_id"]

    assert responses.calls[0]["store"] is False
    assert responses.calls[1]["store"] is False
    assert responses.calls[1]["input"][-3:] == [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Second"},
    ]
