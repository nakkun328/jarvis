"""Structured logging, redaction, request correlation, and failure-path records."""

import io
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.chat.context import Conversation, ConversationStore
from backend.chat.memory_context import MemoryContextError
from backend.chat.persistence import ConversationStorageError
from backend.chat.service import ChatService
from backend.core import logging as jarvis_logging
from backend.core.config import Settings
from backend.core.logging import (
    DROPPED_EVENT,
    REDACTED,
    JsonLogFormatter,
    configure_logging,
    current_log_context,
    log_context,
    redact_text,
)
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError

# Credential-shaped test values are assembled at runtime so no literal looks like a real secret.
FAKE_OPENAI = "sk-" + "Zq7" * 10
FAKE_GOOGLE = "AIza" + "Ab9_" * 9
FAKE_BEARER = "Bearer " + "tok0123456789abcdef"
FAKE_PEM = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKfake\nbody\n-----END "
    + "RSA PRIVATE KEY-----"
)
FAKE_PLAIN = "hunter2-fake-value"
USER_TEXT = "LOGTEST user message that must stay out of logs"
REPLY_TEXT = "LOGTEST provider reply that must stay out of logs"
ALL_SECRETS = (FAKE_OPENAI, FAKE_GOOGLE, FAKE_BEARER.split()[1], "MIIBOgIBAAJBAKfake", FAKE_PLAIN)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.error is not None:
            raise self.error
        return CompletionResponse(REPLY_TEXT, self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        if self.error is not None:
            raise self.error
        yield "LOGTEST delta one "
        yield "LOGTEST delta two"


class FailingStore(ConversationStore):
    @asynccontextmanager
    async def open(self, conversation_id: Any) -> AsyncIterator[tuple[uuid.UUID, Conversation]]:
        raise ConversationStorageError(f"disk detail {FAKE_OPENAI}")
        yield  # pragma: no cover


class FailingMemory:
    async def for_query(self, query: str) -> str | None:
        raise MemoryContextError(f"memory detail {FAKE_PLAIN}")


def make_record(msg: str = "event.name", *args: object, **extra: Any) -> logging.LogRecord:
    made = logging.LogRecord("test.logger", logging.INFO, __file__, 1, msg, args or None, None)
    made.__dict__.update(extra)
    return made


def fmt(msg: str = "event.name", *args: object, **extra: Any) -> dict[str, Any]:
    line = JsonLogFormatter().format(make_record(msg, *args, **extra))
    assert "\n" not in line
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def parse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def capture() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Capture JSON log lines from the root logger for code that does not run the lifespan."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield lambda: parse(stream.getvalue())
    root.removeHandler(handler)


def app_client(tmp_path: Path, provider: FakeProvider | None = None) -> TestClient:
    settings = Settings(db_path=tmp_path / "log.sqlite3")
    return TestClient(create_app(settings, provider or FakeProvider()))


def app_events(raw: str) -> list[dict[str, Any]]:
    """Parse JARVIS records, skipping the HTTP test client's own request logger."""
    return [entry for entry in parse(raw) if entry["logger"] != "httpx"]


def access_records(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry["event"] == "http.request"]


# --- Format ------------------------------------------------------------------------------------


def test_every_line_is_json_with_fixed_fields() -> None:
    entry = fmt("hello %s", "world", answer=42, nested={"a": [1, 2]})
    assert entry["event"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.logger"
    assert entry["answer"] == 42
    assert entry["nested"] == {"a": [1, 2]}
    stamp = datetime.fromisoformat(entry["timestamp"])
    assert stamp.utcoffset() == timedelta(0)
    assert entry["timestamp"].endswith("Z")
    # fmt() asserts the serialized line has no raw newline, so log forging via messages fails.
    forged = "multi\nline\r\nmessage" + chr(0x2028) + "x"
    assert fmt(forged)["event"] == forged


def test_extra_fields_are_bounded_and_never_stringify_objects() -> None:
    class Leaky:
        def __str__(self) -> str:
            return "LEAKED-STR"

    deep: dict[str, Any] = {"v": 1}
    for _ in range(10):
        deep = {"deeper": deep}
    many = {f"k{i}": i for i in range(100)}
    entry = fmt(
        "bounded",
        long="x" * 10_000,
        obj=Leaky(),
        blob=b"raw-bytes",
        deep=deep,
        many=many,
        items=list(range(100)),
        bad_float=float("nan"),
        event_id=uuid.UUID(int=1),
        **{f"f{i}": i for i in range(40)},
    )
    assert len(entry["long"]) < 2100 and "chars]" in entry["long"]
    assert entry["obj"] == "<Leaky>"
    assert entry["blob"] == "<bytes len=9>"
    assert "<max depth>" in json.dumps(entry["deep"])
    assert len(entry["many"]) == 21 and len(entry["items"]) == 21
    assert entry["bad_float"] == "nan"
    assert entry["event_id"] == str(uuid.UUID(int=1))
    assert entry["fields_dropped"] > 0
    assert "LEAKED-STR" not in json.dumps(entry)


def test_extras_cannot_overwrite_fixed_fields() -> None:
    entry = fmt("real.event", level="FORGED", event="forged", logger_name="x", timestamp="t")
    assert entry["level"] == "INFO" and entry["event"] == "real.event"
    assert entry["extra_level"] == "FORGED" and entry["extra_event"] == "forged"


# --- Context -----------------------------------------------------------------------------------


def test_log_context_nests_restores_and_validates() -> None:
    assert current_log_context() == {}
    with log_context(request_id="outer-id", task_id=7, bad=object(), level="x"):  # type: ignore[arg-type]
        assert current_log_context() == {"request_id": "outer-id", "task_id": 7}
        entry = fmt("inside")
        assert entry["request_id"] == "outer-id" and entry["task_id"] == 7
        with log_context(request_id="inner-id"):
            assert fmt("deeper")["request_id"] == "inner-id"
        assert current_log_context()["request_id"] == "outer-id"
    assert current_log_context() == {}
    assert "request_id" not in fmt("outside")


def test_context_values_are_redacted() -> None:
    with log_context(note=f"api_key={FAKE_PLAIN}", auth_token="whatever"):
        entry = fmt("ctx")
    assert FAKE_PLAIN not in json.dumps(entry)
    assert entry["auth_token"] == REDACTED


# --- Redaction ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"key {FAKE_OPENAI} end",
        f"key {FAKE_GOOGLE} end",
        f"header {FAKE_BEARER} end",
        f"Authorization: {FAKE_BEARER}",
        "Authorization: Basic " + FAKE_PLAIN,
        f"api_key={FAKE_PLAIN}",
        f"API-KEY: {FAKE_PLAIN}",
        f'{{"password": "{FAKE_PLAIN}"}}',
        f"client_secret = '{FAKE_PLAIN}'",
        f"OPENAI_API_KEY={FAKE_PLAIN} next",
        f"before {FAKE_PEM} after",
        "before " + FAKE_PEM.split("\n")[0] + "\nMIIBOgIBAAJBAKfake truncated",
        "ghp_" + "a1" * 15,
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.sigsigsig",
    ],
)
def test_redact_text_known_patterns(text: str) -> None:
    redacted = redact_text(text)
    for secret in (*ALL_SECRETS, "ghp_", "AKIA", "eyJhbGci", "Basic " + FAKE_PLAIN):
        assert secret not in redacted
    assert "[REDACTED" in redacted


def test_redaction_keeps_ordinary_text() -> None:
    text = "task-force alpha used 12 tokens in 3.5ms; sk-learn is fine; max_tokens skipped"
    assert redact_text("task-force alpha ran for 3.5ms") == "task-force alpha ran for 3.5ms"
    assert "task-force" in redact_text(text)


def test_redaction_in_message_args_extra_and_nested_dicts() -> None:
    nested = {
        "ok": "fine",
        "Authorization": FAKE_BEARER,
        "headers": {"X-Api-Key": FAKE_PLAIN, "cookie": "sid=abc", "accept": "json"},
        "items": [{"password": FAKE_PLAIN}, f"see {FAKE_OPENAI}"],
        "free": f"token={FAKE_PLAIN}",
    }
    entry = fmt(
        "failed with %s and key %s",
        FAKE_GOOGLE,
        FAKE_BEARER,
        payload=nested,
        note=f"contains {FAKE_OPENAI}",
        **{"api_key": FAKE_PLAIN, "Password": FAKE_PLAIN},
        completion_tokens=5,
    )
    dumped = json.dumps(entry)
    for secret in ALL_SECRETS:
        assert secret not in dumped
    assert entry["api_key"] == entry["Password"] == REDACTED
    assert entry["completion_tokens"] == 5  # counts are not credentials
    assert entry["payload"]["ok"] == "fine"
    assert entry["payload"]["headers"]["accept"] == "json"
    assert entry["payload"]["headers"]["X-Api-Key"] == REDACTED
    assert entry["payload"]["Authorization"] == REDACTED


def test_exception_logging_emits_type_and_frames_not_messages(
    capture: Callable[[], list[dict[str, Any]]],
) -> None:
    log = logging.getLogger("test.exceptions")

    def inner() -> None:
        raise ValueError(f"upstream payload {FAKE_OPENAI} {USER_TEXT}")

    try:
        try:
            inner()
        except ValueError as exc:
            raise ProviderError(f"wrapped {FAKE_PLAIN}") from exc
    except ProviderError:
        log.exception("op.failed")
        log.error("with stack", exc_info=True, stack_info=True)

    first = capture()[0]
    dumped = json.dumps(capture())
    for secret in (FAKE_OPENAI, FAKE_PLAIN, USER_TEXT, "upstream payload", "wrapped"):
        assert secret not in dumped
    assert first["event"] == "op.failed"
    assert first["error_type"] == "ProviderError"
    assert first["error_chain"] == ["ValueError"]
    assert any(
        frame.endswith("in test_exception_logging_emits_type_and_frames_not_messages")
        and frame.startswith("tests/test_logging.py:")
        for frame in first["traceback"]
    )
    assert any(frame.endswith("in inner") for frame in first["cause_traceback"])
    assert all(not frame.startswith("/") for frame in first["traceback"] + first["cause_traceback"])


def test_redaction_failure_drops_fields_instead_of_emitting_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(text: str) -> str:
        raise RuntimeError("redactor down")

    monkeypatch.setattr(jarvis_logging, "redact_text", broken)
    entry = fmt(f"message {FAKE_OPENAI}", field=f"value {FAKE_OPENAI}", count=3, flag=True)
    assert entry["event"] == DROPPED_EVENT
    assert "field" not in entry
    assert entry["count"] == 3 and entry["flag"] is True
    assert FAKE_OPENAI not in json.dumps(entry)


def test_formatter_never_raises_on_bad_records() -> None:
    bad = make_record("needs %s and %s", "only-one")
    line = JsonLogFormatter().format(bad)
    assert json.loads(line)["level"] == "INFO"


# --- configure_logging -------------------------------------------------------------------------


def test_configure_logging_is_idempotent_and_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    configure_logging("INFO")
    configure_logging("WARNING")
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    assert root.level == logging.WARNING
    logging.getLogger("test.configure").info("dropped by level")
    logging.getLogger("test.configure").warning("kept", extra={"count": 1})
    entries = parse(capsys.readouterr().err)
    assert [entry["event"] for entry in entries] == ["kept"]
    assert entries[0]["count"] == 1


def test_configure_logging_routes_uvicorn_through_json_handler() -> None:
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(logging.NullHandler())
    uvicorn_logger.propagate = False
    configure_logging("INFO")
    assert uvicorn_logger.handlers == [] and uvicorn_logger.propagate
    access = logging.getLogger("uvicorn.access")
    assert access.disabled and not access.propagate


def test_configure_logging_rejects_unknown_level() -> None:
    with pytest.raises(ValueError):
        configure_logging("LOUD")


def test_logging_fixture_does_not_leak_handlers() -> None:
    # restore_logging_state (tests/conftest.py) returns the root logger to pytest's handlers.
    assert not any(
        isinstance(handler.formatter, JsonLogFormatter) for handler in logging.getLogger().handlers
    )


# --- Request correlation and access log --------------------------------------------------------


def test_generated_request_id_is_returned_and_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with app_client(tmp_path) as client:
        response = client.get("/health/live")
    request_id = response.headers["X-Request-ID"]
    assert uuid.UUID(request_id).version == 4
    access = access_records(parse(capsys.readouterr().err))
    assert len(access) == 1
    assert access[0]["request_id"] == request_id
    assert access[0]["method"] == "GET" and access[0]["path"] == "/health/live"
    assert access[0]["status"] == 200
    assert isinstance(access[0]["duration_ms"], int | float) and access[0]["duration_ms"] >= 0
    assert "error_type" not in access[0]


def test_valid_inbound_request_id_is_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with app_client(tmp_path) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "client-req.01_A"})
    assert response.headers["X-Request-ID"] == "client-req.01_A"
    assert access_records(parse(capsys.readouterr().err))[0]["request_id"] == "client-req.01_A"


@pytest.mark.parametrize(
    "bad",
    ["short", "x" * 65, "has space in it", "bad/char/slash", "semi;colon-id", ""],
)
def test_malformed_inbound_request_id_is_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    with app_client(tmp_path) as client:
        response = client.get("/health/live", headers={"X-Request-ID": bad})
    request_id = response.headers["X-Request-ID"]
    assert request_id != bad and uuid.UUID(request_id).version == 4
    assert access_records(parse(capsys.readouterr().err))[0]["request_id"] == request_id


def test_duplicate_request_id_headers_are_replaced(tmp_path: Path) -> None:
    with app_client(tmp_path) as client:
        response = client.get(
            "/health/live",
            headers=[("X-Request-ID", "first-id-1234"), ("X-Request-ID", "second-id-1234")],
        )
    assert uuid.UUID(response.headers["X-Request-ID"]).version == 4


def test_access_log_uses_route_template_and_omits_query_string(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(Settings(db_path=tmp_path / "log.sqlite3"), FakeProvider())

    @app.get("/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    with TestClient(app) as client:
        client.get(f"/items/abc123?api_key={FAKE_PLAIN}&q={USER_TEXT}")
        client.get("/missing")
    entries = app_events(capsys.readouterr().err)
    access = access_records(entries)
    assert [(a["path"], a["status"]) for a in access] == [
        ("/items/{item_id}", 200),
        ("/missing", 404),
    ]
    dumped = json.dumps(entries)
    assert "abc123" not in dumped and FAKE_PLAIN not in dumped and USER_TEXT not in dumped


def test_unhandled_exception_is_logged_with_type_and_sanitized_frames(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(Settings(db_path=tmp_path / "log.sqlite3"), FakeProvider())

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"private detail {FAKE_OPENAI}")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    raw = capsys.readouterr().err
    (entry,) = access_records(parse(raw))
    assert entry["status"] == 500 and entry["level"] == "ERROR"
    assert entry["error_type"] == "RuntimeError"
    assert any(frame.endswith("in boom") for frame in entry["traceback"])
    assert "private detail" not in raw and FAKE_OPENAI not in raw


def test_chat_call_logs_no_bodies_headers_or_provider_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with app_client(tmp_path) as client:
        response = client.post(
            "/api/chat",
            json={"message": USER_TEXT},
            headers={
                "Authorization": FAKE_BEARER,
                "X-Api-Key": FAKE_PLAIN,
                "Cookie": "session=fake-session-value",
            },
        )
    assert response.status_code == 200 and response.json()["reply"] == REPLY_TEXT
    raw = capsys.readouterr().err
    for leaked in (USER_TEXT, REPLY_TEXT, FAKE_PLAIN, FAKE_BEARER, "fake-session-value", "LOGTEST"):
        assert leaked not in raw
    (entry,) = access_records(parse(raw))
    assert entry["path"] == "/api/chat" and entry["method"] == "POST" and entry["status"] == 200


def test_sse_request_has_header_and_one_correlated_access_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with app_client(tmp_path) as client:
        response = client.post(
            "/api/chat/stream",
            json={"message": USER_TEXT},
            headers={"X-Request-ID": "sse-request-0001"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Request-ID"] == "sse-request-0001"
    assert "event: delta" in response.text and "event: done" in response.text
    raw = capsys.readouterr().err
    assert "LOGTEST" not in raw
    entries = app_events(raw)
    (access,) = access_records(entries)
    assert access["request_id"] == "sse-request-0001"
    assert access["path"] == "/api/chat/stream" and access["status"] == 200
    # No per-delta logging: only startup and the single access record are present.
    events = [entry["event"] for entry in entries]
    assert events[0] == "JARVIS backend started" and events[-1] == "http.request"
    assert all(event.startswith("Personality (") for event in events[1:-1])


def test_provider_failure_logs_event_type_and_duration_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider(ProviderError(f"upstream said {FAKE_OPENAI} {USER_TEXT}"))
    with app_client(tmp_path, provider) as client:
        regular = client.post(
            "/api/chat", json={"message": USER_TEXT}, headers={"X-Request-ID": "fail-request-001"}
        )
        stream = client.post(
            "/api/chat/stream",
            json={"message": USER_TEXT},
            headers={"X-Request-ID": "fail-stream-0001"},
        )
    assert regular.status_code == 502
    assert "event: error" in stream.text
    raw = capsys.readouterr().err
    for leaked in (FAKE_OPENAI, USER_TEXT, "upstream said"):
        assert leaked not in raw
    failures = [e for e in parse(raw) if e["event"] == "chat.provider_failed"]
    assert [(e["request_id"], e["streaming"]) for e in failures] == [
        ("fail-request-001", False),
        ("fail-stream-0001", True),
    ]
    for failure in failures:
        assert failure["error_type"] == "ProviderError"
        assert failure["level"] == "WARNING" and failure["duration_ms"] >= 0
        assert set(failure) == {
            "timestamp",
            "level",
            "logger",
            "event",
            "request_id",
            "error_type",
            "duration_ms",
            "streaming",
        }


@pytest.mark.parametrize("streaming", [False, True])
def test_storage_and_memory_failures_log_event_and_type_only(
    capture: Callable[[], list[dict[str, Any]]], streaming: bool
) -> None:
    import asyncio

    async def run(service: ChatService) -> None:
        if streaming:
            async for _ in service.stream(USER_TEXT):
                pass
        else:
            await service.complete(USER_TEXT)

    storage = ChatService(FakeProvider(), FailingStore())
    with pytest.raises(ConversationStorageError):
        asyncio.run(run(storage))
    memory = ChatService(FakeProvider(), memory_context=FailingMemory())  # type: ignore[arg-type]
    with pytest.raises(MemoryContextError):
        asyncio.run(run(memory))

    entries = [e for e in capture() if e["logger"].startswith("backend.")]
    assert [(e["event"], e["error_type"], e["streaming"]) for e in entries] == [
        ("chat.storage_failed", "ConversationStorageError", streaming),
        ("chat.memory_context_failed", "MemoryContextError", streaming),
    ]
    dumped = json.dumps(entries)
    for leaked in (FAKE_OPENAI, FAKE_PLAIN, USER_TEXT, "disk detail", "memory detail"):
        assert leaked not in dumped


def test_client_errors_are_not_logged_as_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with app_client(tmp_path) as client:
        missing = client.post(
            "/api/chat", json={"message": "hi", "conversation_id": str(uuid.uuid4())}
        )
    assert missing.status_code == 404
    events = [entry["event"] for entry in app_events(capsys.readouterr().err)]
    assert events[0] == "JARVIS backend started" and events[-1] == "http.request"
    assert all(event.startswith("Personality (") for event in events[1:-1])


def test_redact_text_is_idempotent_and_bounded_on_hostile_input() -> None:
    hostile = "api_key" * 20_000 + "-" * 5000
    started = time.perf_counter()
    once = redact_text(hostile)
    assert time.perf_counter() - started < 2
    assert redact_text(once) == once


def test_request_id_reaches_sync_async_and_streaming_handlers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fastapi.responses import StreamingResponse

    app = create_app(Settings(db_path=tmp_path / "log.sqlite3"), FakeProvider())
    log = logging.getLogger("test.handlers")

    @app.get("/sync")
    def sync_handler() -> dict[str, str]:  # runs in a worker thread
        log.info("in.sync")
        return {}

    @app.get("/async")
    async def async_handler() -> dict[str, str]:
        log.info("in.async")
        return {}

    @app.get("/body")
    async def streaming_handler() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            log.info("in.stream")
            yield "x"

        return StreamingResponse(body())

    with TestClient(app) as client:
        paths = ("/sync", "/async", "/body")
        ids = {path: client.get(path).headers["X-Request-ID"] for path in paths}
    by_event = {
        e["event"]: e for e in app_events(capsys.readouterr().err) if e["logger"] == "test.handlers"
    }
    assert by_event["in.sync"]["request_id"] == ids["/sync"]
    assert by_event["in.async"]["request_id"] == ids["/async"]
    assert by_event["in.stream"]["request_id"] == ids["/body"]
