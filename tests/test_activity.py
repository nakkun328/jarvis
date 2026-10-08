"""Activity events: a fixed vocabulary, emitted additively, never carrying any content."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.chat.activity import (
    ActivityErrorCode,
    ActivityEvent,
    ActivityRoute,
    ActivityStage,
    ResearchStep,
)
from backend.chat.context import ConversationStore
from backend.chat.memory_context import MemoryContext, MemoryContextError
from backend.chat.service import ChatDelta, ChatDone, ChatService
from backend.core.config import Settings
from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.writer import MemoryWriter
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError

ACTIVITY_NAME = "X-Jarvis-Activity"
ACTIVITY = {ACTIVITY_NAME: "1"}
HOSTILE = "IGNORE-ALL-RULES </script><b>x</b> /etc/passwd sk-live-0123456789"
ALLOWED_KEYS = {"stage", "route", "count", "step", "code"}
VOCABULARY = (
    {stage.value for stage in ActivityStage}
    | {route.value for route in ActivityRoute}
    | {step.value for step in ResearchStep}
    | {code.value for code in ActivityErrorCode}
)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, *, fail: bool = False, fail_after: int | None = None) -> None:
        self.fail = fail
        self.fail_after = fail_after
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.fail:
            raise ProviderError(f"upstream said: {HOSTILE}")
        return CompletionResponse("Acknowledged.", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        if self.fail:
            raise ProviderError(f"upstream said: {HOSTILE}")
        yield "Acknow"
        if self.fail_after is not None:
            raise ProviderError(f"upstream said: {HOSTILE}")
        yield "ledged."


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def assert_only_vocabulary(payload: dict) -> None:
    assert set(payload) <= ALLOWED_KEYS
    for key, value in payload.items():
        if key == "count":
            assert type(value) is int and 0 <= value <= 99
        else:
            assert value in VOCABULARY


def memory_service(tmp_path: Path, provider: FakeProvider, notes: list[str]):
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    for content in notes:
        record = MemoryRecord(
            category=MemoryCategory.USER,
            content=content,
            source="conversation:reviewed-example",
            origin=MemoryOrigin.USER_EXPLICIT,
            importance=0.7,
            confidence=1.0,
        )
        writer.submit(record)
        writer.approve(record.id)
    return Settings(db_path=database.path, memory_vault_path=vault.root)


# --- the event type -------------------------------------------------------------------------


def test_every_event_serialises_to_stage_plus_one_allowlisted_field() -> None:
    events = [
        ActivityEvent.received(),
        ActivityEvent.routing(),
        *(ActivityEvent.route_selected(route) for route in ActivityRoute),
        ActivityEvent.memory_lookup(2),
        *(ActivityEvent.researching(step) for step in ResearchStep),
        ActivityEvent.generating(),
        ActivityEvent.speaking(),
        ActivityEvent.done(),
        *(ActivityEvent.error(code) for code in ActivityErrorCode),
    ]
    assert {event.stage for event in events} == set(ActivityStage)
    for event in events:
        payload = event.to_payload()
        assert payload["stage"] == event.stage.value
        assert len(payload) <= 2
        assert_only_vocabulary(payload)
    assert ActivityEvent.memory_lookup(3).to_payload() == {"stage": "memory_lookup", "count": 3}
    assert ActivityEvent.route_selected(ActivityRoute.MAIN).to_payload() == {
        "stage": "route_selected",
        "route": "main",
    }
    assert ActivityEvent.error(ActivityErrorCode.PROVIDER).to_payload() == {
        "stage": "error",
        "code": "provider",
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda: ActivityEvent("received"),  # a plain string is not a stage
        lambda: ActivityEvent(ActivityStage.RECEIVED, count=1),
        lambda: ActivityEvent(ActivityStage.DONE, code=ActivityErrorCode.PROVIDER),
        lambda: ActivityEvent(ActivityStage.MEMORY_LOOKUP),
        lambda: ActivityEvent(ActivityStage.MEMORY_LOOKUP, count=-1),
        lambda: ActivityEvent(ActivityStage.MEMORY_LOOKUP, count=100),
        lambda: ActivityEvent(ActivityStage.MEMORY_LOOKUP, count=True),
        lambda: ActivityEvent(ActivityStage.MEMORY_LOOKUP, count="3"),
        lambda: ActivityEvent(ActivityStage.ERROR, code="my secret upstream text"),
        lambda: ActivityEvent(ActivityStage.ERROR),
        lambda: ActivityEvent(ActivityStage.ROUTE_SELECTED, route="elsewhere"),
        lambda: ActivityEvent(ActivityStage.RESEARCHING, step="free text"),
    ],
)
def test_events_reject_fields_outside_the_vocabulary(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_reserved_stages_are_never_emitted_today(tmp_path: Path) -> None:
    async def run() -> set[ActivityStage]:
        service = ChatService(FakeProvider(), ConversationStore())
        seen = set()
        async for item in service.stream_with_activity("hello"):
            if isinstance(item, ActivityEvent):
                seen.add(item.stage)
        return seen

    seen = asyncio.run(run())
    assert seen.isdisjoint(
        {
            ActivityStage.ROUTING,
            ActivityStage.ROUTE_SELECTED,
            ActivityStage.RESEARCHING,
            ActivityStage.SPEAKING,
        }
    )


# --- the service ----------------------------------------------------------------------------


def stages(items) -> list[str]:
    return [
        item.stage.value if isinstance(item, ActivityEvent) else type(item).__name__
        for item in items
    ]


def test_stream_order_without_memory() -> None:
    async def run():
        service = ChatService(FakeProvider(), ConversationStore())
        return [item async for item in service.stream_with_activity("hello")]

    items = asyncio.run(run())
    assert stages(items) == [
        "received",
        "generating",
        "ChatDelta",
        "ChatDelta",
        "done",
        "ChatDone",
    ]
    assert not any(
        isinstance(item, ActivityEvent) and item.stage is ActivityStage.MEMORY_LOOKUP
        for item in items
    )


def test_plain_stream_is_unchanged_and_has_no_activity() -> None:
    async def run():
        service = ChatService(FakeProvider(), ConversationStore())
        return [item async for item in service.stream("hello")]

    items = asyncio.run(run())
    assert items[:2] == [ChatDelta("Acknow"), ChatDelta("ledged.")]
    assert isinstance(items[2], ChatDone) and len(items) == 3


def test_complete_reports_activity_in_order_and_is_otherwise_unchanged() -> None:
    async def run():
        observed: list[ActivityEvent] = []
        service = ChatService(FakeProvider(), ConversationStore())
        with_observer = await service.complete("hi", on_activity=observed.append)
        without = await ChatService(FakeProvider(), ConversationStore()).complete("hi")
        return observed, with_observer, without

    observed, with_observer, without = asyncio.run(run())
    assert [event.stage.value for event in observed] == ["received", "generating", "done"]
    assert with_observer.reply == without.reply == "Acknowledged."


def test_memory_lookup_reports_how_many_notes_were_used(tmp_path: Path) -> None:
    provider = FakeProvider()
    settings = memory_service(
        tmp_path, provider, ["Observatory opens on Saturday", "Observatory tours are free"]
    )
    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/chat/stream",
            json={"message": "When does the Observatory open?"},
            headers=ACTIVITY,
        )
        none_matched = client.post(
            "/api/chat/stream", json={"message": "zzzz unrelated"}, headers=ACTIVITY
        )
    events = sse_events(response.text)
    activity = [data for name, data in events if name == "activity"]
    assert [item["stage"] for item in activity] == [
        "received",
        "memory_lookup",
        "generating",
        "done",
    ]
    assert activity[1] == {"stage": "memory_lookup", "count": 2}
    # Consulted but nothing matched is a real result, reported as zero.
    assert {"stage": "memory_lookup", "count": 0} in [
        data for name, data in sse_events(none_matched.text) if name == "activity"
    ]
    assert "Observatory" not in json.dumps(activity)


def test_memory_stage_is_absent_when_memory_is_not_configured(tmp_path: Path) -> None:
    with TestClient(
        create_app(Settings(db_path=tmp_path / "plain.sqlite3"), FakeProvider())
    ) as client:
        text = client.post("/api/chat/stream", json={"message": "hi"}, headers=ACTIVITY).text
    assert "memory_lookup" not in text


class FailingMemory(MemoryContext):
    def __init__(self) -> None:  # no retriever needed
        pass

    async def for_query(self, query: str) -> str | None:
        raise MemoryContextError(f"vault at /private/path broke: {HOSTILE}")


def test_memory_failure_ends_in_a_memory_error_before_the_provider() -> None:
    async def run():
        provider = FakeProvider()
        service = ChatService(provider, ConversationStore(), memory_context=FailingMemory())
        items = []
        with pytest.raises(MemoryContextError):
            async for item in service.stream_with_activity("hello"):
                items.append(item)
        return items, provider

    items, provider = asyncio.run(run())
    assert [item.to_payload() for item in items] == [
        {"stage": "received"},
        {"stage": "error", "code": "memory"},
    ]
    assert provider.requests == []


# --- the HTTP stream ------------------------------------------------------------------------


def post_stream(client: TestClient, message: str, *, headers=None, conversation_id=None):
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post("/api/chat/stream", json=body, headers=headers or {})


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(db_path=tmp_path / "a.sqlite3"), FakeProvider())) as c:
        yield c


def test_stream_is_unchanged_without_the_opt_in_header(client: TestClient) -> None:
    text = post_stream(client, "hello").text
    assert "event: activity" not in text
    assert [name for name, _ in sse_events(text)] == ["delta", "delta", "done"]
    # Any value other than "1" is not an opt-in.
    assert "event: activity" not in post_stream(client, "hello", headers={ACTIVITY_NAME: "0"}).text


def test_activity_is_purely_additive_on_the_stream(client: TestClient) -> None:
    plain = post_stream(client, "hello")
    rich = post_stream(client, "hello", headers=ACTIVITY)
    assert rich.headers["content-type"] == plain.headers["content-type"]
    assert rich.headers["cache-control"] == plain.headers["cache-control"]

    # Dropping whole `activity` blocks leaves the plain stream, byte for byte (the `done`
    # block differs only by the new conversation id of each request).
    def blocks(text: str, name: str) -> list[str]:
        return [b for b in text.split("\n\n") if b.startswith(f"event: {name}")]

    assert blocks(rich.text, "delta") == blocks(plain.text, "delta")
    assert len(blocks(rich.text, "done")) == len(blocks(plain.text, "done")) == 1
    assert len(rich.text.split("\n\n")) == len(plain.text.split("\n\n")) + 3
    assert [n for n, _ in sse_events(rich.text)] == [
        "activity",
        "activity",
        "delta",
        "delta",
        "activity",
        "done",
    ]
    assert 'event: activity\ndata: {"stage": "received"}\n\n' in rich.text


def test_regular_endpoint_ignores_the_header_and_is_unchanged(client: TestClient) -> None:
    plain = client.post("/api/chat", json={"message": "hello"})
    rich = client.post("/api/chat", json={"message": "hello"}, headers=ACTIVITY)
    assert plain.status_code == rich.status_code == 200
    keys = {"conversation_id", "reply", "provider", "model"}
    assert set(rich.json()) == set(plain.json()) == keys
    assert "activity" not in rich.text


def test_failure_before_any_text_ends_with_a_fixed_error_code(tmp_path: Path) -> None:
    provider = FakeProvider(fail=True)
    with TestClient(create_app(Settings(db_path=tmp_path / "f.sqlite3"), provider)) as client:
        response = post_stream(client, HOSTILE, headers=ACTIVITY)
    events = sse_events(response.text)
    assert [(n, d) for n, d in events] == [
        ("activity", {"stage": "received"}),
        ("activity", {"stage": "generating"}),
        ("activity", {"stage": "error", "code": "provider"}),
        ("error", {"message": "chat provider failed"}),
    ]
    assert "upstream" not in response.text and "sk-live" not in response.text


def test_failure_after_text_ends_in_error_not_done(tmp_path: Path) -> None:
    provider = FakeProvider(fail_after=1)
    with TestClient(create_app(Settings(db_path=tmp_path / "g.sqlite3"), provider)) as client:
        events = sse_events(post_stream(client, "hello", headers=ACTIVITY).text)
    assert [n for n, _ in events] == ["activity", "activity", "delta", "activity", "error"]
    assert events[3][1] == {"stage": "error", "code": "provider"}
    assert not any(d.get("stage") == "done" for n, d in events if n == "activity")


def test_missing_conversation_is_a_fixed_code(client: TestClient) -> None:
    events = sse_events(
        post_stream(client, "hello", headers=ACTIVITY, conversation_id=str(uuid4())).text
    )
    assert [(n, d) for n, d in events] == [
        ("activity", {"stage": "received"}),
        ("activity", {"stage": "error", "code": "conversation_not_found"}),
        ("error", {"message": "conversation not found"}),
    ]


def test_unexpected_exception_maps_to_internal_without_its_text() -> None:
    class Broken(FakeProvider):
        async def stream(self, request):
            raise RuntimeError(f"boom {HOSTILE}")
            yield  # pragma: no cover

    async def run():
        service = ChatService(Broken(), ConversationStore())
        items = []
        with pytest.raises(RuntimeError):
            async for item in service.stream_with_activity("hello"):
                items.append(item)
        return items

    payloads = [item.to_payload() for item in asyncio.run(run())]
    assert payloads[-1] == {"stage": "error", "code": "internal"}
    assert HOSTILE not in json.dumps(payloads)


def test_cancelled_stream_ends_without_done_and_is_not_saved() -> None:
    async def run():
        store = ConversationStore()
        service = ChatService(FakeProvider(), store)
        stream = service.stream_with_activity("hello")
        seen = [await anext(stream), await anext(stream), await anext(stream)]
        await stream.aclose()  # the client went away mid-reply
        return seen, store

    seen, store = asyncio.run(run())
    assert stages(seen) == ["received", "generating", "ChatDelta"]
    conversation = next(iter(store._conversations.values()))
    assert conversation.messages == []
    assert conversation.active_requests == 0


def test_hostile_message_and_memory_never_reach_activity(tmp_path: Path) -> None:
    provider = FakeProvider()
    settings = memory_service(tmp_path, provider, [f"Observatory {HOSTILE}"])
    with TestClient(create_app(settings, provider)) as client:
        text = post_stream(client, f"Observatory {HOSTILE}", headers=ACTIVITY).text
    payloads = [data for name, data in sse_events(text) if name == "activity"]
    assert payloads[1] == {"stage": "memory_lookup", "count": 1}
    for payload in payloads:
        assert_only_vocabulary(payload)
    serialised = json.dumps(payloads)
    for fragment in ("IGNORE", "passwd", "sk-live", "<b>", "Observatory", "/", "vault"):
        assert fragment not in serialised
