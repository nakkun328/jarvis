"""Backend contract the chat UI relies on, exercised through the dev-only fake provider."""

import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "dev_fake_provider_server", ROOT / "scripts" / "dev_fake_provider_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


def _events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


@pytest.fixture
def client(tmp_path: Path):
    provider_app = harness.build_dev_app(tmp_path / "chat.sqlite3")
    with TestClient(provider_app) as test_client:
        yield test_client


def stream(client: TestClient, message: str, conversation_id: str | None = None):
    body = {"message": message, **({"conversation_id": conversation_id} if conversation_id else {})}
    response = client.post("/api/chat/stream", json=body)
    assert response.status_code == 200
    return _events(response.text)


def test_normal_stream_has_incremental_deltas_then_done(client: TestClient) -> None:
    events = stream(client, "hello")
    names = [name for name, _ in events]
    assert names[-1] == "done"
    assert names[:-1] == ["delta"] * (len(names) - 1) and len(names) > 2
    assert events[-1][1]["provider"] == "fake"
    assert events[-1][1]["conversation_id"]


def test_failure_after_deltas_ends_in_error_without_done(client: TestClient) -> None:
    events = stream(client, "/fail-after 2")
    assert [name for name, _ in events] == ["delta", "delta", "error"]
    assert events[-1][1] == {"message": "chat provider failed"}


def test_failure_before_first_delta_is_only_an_error(client: TestClient) -> None:
    assert stream(client, "/fail-now") == [("error", {"message": "chat provider failed"})]


def test_failed_and_empty_turns_do_not_enter_conversation_context(client: TestClient) -> None:
    first = stream(client, "hello")
    conversation_id = first[-1][1]["conversation_id"]
    stream(client, "/fail-after 2", conversation_id)
    stream(client, "/fail-now", conversation_id)
    stream(client, "/empty", conversation_id)
    history = stream(client, "/history", conversation_id)
    assert history[0][1]["text"] == "履歴メッセージ数: 2"


def test_flaky_provider_fails_once_then_retry_succeeds(client: TestClient) -> None:
    assert stream(client, "/flaky")[-1][0] == "error"
    assert stream(client, "/flaky")[-1][0] == "done"


def test_unknown_conversation_is_an_error_not_a_new_conversation(client: TestClient) -> None:
    unknown = "00000000-0000-4000-8000-000000000000"
    assert stream(client, "hello", unknown) == [("error", {"message": "conversation not found"})]
    response = client.post("/api/chat", json={"message": "hello", "conversation_id": unknown})
    assert response.status_code == 404
    assert response.json() == {"detail": "conversation not found"}


def test_message_limits(client: TestClient) -> None:
    assert client.post("/api/chat", json={"message": "あ" * 4000}).status_code == 200
    assert client.post("/api/chat", json={"message": "あ" * 4001}).status_code == 422
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    blank = client.post("/api/chat/stream", json={"message": "   "})
    assert blank.status_code == 422
    assert blank.json() == {"detail": "message must contain text"}


def test_provider_none_returns_503_for_both_routes(tmp_path: Path) -> None:
    app = harness.build_dev_app(tmp_path / "none.sqlite3", with_provider=False)
    with TestClient(app) as test_client:
        for route in ("/api/chat", "/api/chat/stream"):
            response = test_client.post(route, json={"message": "hi"})
            assert response.status_code == 503
            assert response.json() == {"detail": "chat provider is not configured"}
        assert test_client.get("/").status_code == 200


def test_frontend_error_table_matches_backend_strings() -> None:
    """The UI maps these exact English strings to Japanese; keep both sides in sync."""
    api_js = (ROOT / "frontend" / "chat-api.js").read_text(encoding="utf-8")
    table = api_js.split("const SERVER_MESSAGES = {", 1)[1].split("\n};", 1)[0]
    frontend_keys = set(re.findall(r'^  "([^"]+)": \{', table, flags=re.MULTILINE))
    chat_py = (ROOT / "backend" / "api" / "chat.py").read_text(encoding="utf-8")
    backend = set(re.findall(r'detail="([^"]+)"', chat_py))
    backend |= set(re.findall(r'_sse\("error", \{"message": "([^"]+)"\}\)', chat_py))
    assert backend, "no backend error strings found; update this contract test"
    assert backend == frontend_keys


def test_ui_markup_has_stop_control_and_custom_validation() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="stop"' in html
    assert "novalidate" in html
    assert 'maxlength="4000"' in html


def test_dev_harness_is_dev_only(tmp_path: Path) -> None:
    for non_loopback in ("0.0.0.0", "192.0.2.1", "example.com"):
        with pytest.raises(SystemExit):
            harness._require_loopback(non_loopback)
    harness._require_loopback("127.0.0.1")
    harness._require_loopback("::1")
    with pytest.raises(ValueError):
        harness.build_dev_app(Path("relative.sqlite3"))
    for source in (ROOT / "backend").rglob("*.py"):
        assert "dev_fake_provider_server" not in source.read_text(encoding="utf-8")
