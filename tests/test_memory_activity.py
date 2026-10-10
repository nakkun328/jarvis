"""Memory activity feed: ring buffer, publisher wiring, failure isolation, endpoint, auth.

Fakes only: temporary SQLite files and vaults, a scripted provider, no network.
"""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.memory import create_memory_router
from backend.api.memory_activity import create_memory_activity_router
from backend.api.memory_withdraw import create_memory_withdraw_router
from backend.api.research_memory import create_research_memory_router
from backend.core.database import Database
from backend.memory.auto_approval import AUTO_APPROVER
from backend.memory.chat_auto import ChatAutoMemory
from backend.memory.events import (
    FAILURE_CODE,
    MAX_EVENTS,
    MemoryActivityFeed,
    MemoryEventKind,
    MemoryEventOrigin,
    safe_publish,
    summarize,
)
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.writer import MemoryWriter
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.research.memory_candidates import ResearchMemoryCandidates
from backend.research.models import (
    ResearchLevel,
    ResearchStatus,
    SourceEvaluation,
    SourceType,
)
from backend.research.repository import ResearchRepository

HEADERS = {"X-Jarvis-Confirm": "1"}
MESSAGE = "最近は毎朝Pythonでスクリプトを書くのが習慣になっています"
QUOTE = "毎朝Pythonでスクリプトを書くのが習慣"
FACT = "毎朝Pythonでスクリプトを書く習慣がある"
GOOD_URL = "https://docs.example.test/foo"
CLAIM_QUOTE = "The Foo cache keeps entries for 60 seconds."
RETRIEVED = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def new_id() -> UUID:
    return uuid4()


# ----- the feed itself -----


def test_seq_is_monotonic_and_events_are_oldest_first() -> None:
    feed = MemoryActivityFeed()
    assert feed.latest == 0 and feed.since(0) == []
    for index in range(3):
        feed.publish(MemoryEventKind.STAGED, MemoryEventOrigin.CHAT, new_id(), f"fact {index}")
    assert feed.latest == 3
    events = feed.since(0)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["summary"] for event in events] == ["fact 0", "fact 1", "fact 2"]
    assert [event["seq"] for event in feed.since(2)] == [3]
    assert feed.since(3) == [] and feed.since(99) == []
    assert set(events[0]) == {"seq", "at", "kind", "origin", "memory_id", "summary"}
    assert events[0]["kind"] == "staged" and events[0]["origin"] == "chat"
    assert datetime.fromisoformat(events[0]["at"]).tzinfo is not None


def test_the_buffer_is_bounded_but_seq_keeps_growing() -> None:
    feed = MemoryActivityFeed()
    for index in range(MAX_EVENTS + 25):
        feed.publish(MemoryEventKind.APPROVED, MemoryEventOrigin.RESEARCH, new_id(), str(index))
    assert feed.latest == MAX_EVENTS + 25
    events = [event for event in feed.since(0, limit=10_000)]
    assert len(events) <= 50  # a page is capped
    all_kept = feed._events  # the ring itself
    assert len(all_kept) == MAX_EVENTS
    assert all_kept[0]["seq"] == 26 and all_kept[-1]["seq"] == MAX_EVENTS + 25


def test_a_page_is_at_most_fifty_events() -> None:
    feed = MemoryActivityFeed()
    for _ in range(80):
        feed.publish(MemoryEventKind.STAGED, MemoryEventOrigin.CHAT, new_id(), "x")
    page = feed.since(0)
    assert len(page) == 50 and page[0]["seq"] == 1
    assert feed.since(page[-1]["seq"])[0]["seq"] == 51


def test_summary_is_first_line_cleaned_and_cut_to_eighty() -> None:
    hostile = "ab‮cd​\x00ef gh\n second line <script>alert(1)</script>"
    assert summarize(hostile) == "abcdef gh"  # bidi, zero-width, NUL removed; first line only
    assert "‮" not in summarize("‮" + "x")
    long = "あ" * 200
    assert len(summarize(long)) == 80 and summarize(long).endswith("…")
    assert summarize("a" * 80) == "a" * 80
    assert summarize("") == "" and summarize(None) == ""  # type: ignore[arg-type]
    assert summarize("<img src=x onerror=alert(1)>") == "<img src=x onerror=alert(1)>"


def test_publishing_failures_are_swallowed_with_a_fixed_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("secret fact text")

    with caplog.at_level(logging.WARNING):
        safe_publish(broken, MemoryEventKind.STAGED, MemoryEventOrigin.CHAT, new_id(), "secret")
        safe_publish(None, MemoryEventKind.STAGED, MemoryEventOrigin.CHAT, new_id(), "secret")
    assert [record.code for record in caplog.records] == [FAILURE_CODE]  # type: ignore[attr-defined]
    assert "secret" not in caplog.text


def test_feed_rejects_unknown_kind_and_origin() -> None:
    feed = MemoryActivityFeed()
    with pytest.raises(ValueError):
        feed.publish("bogus", MemoryEventOrigin.CHAT, new_id(), "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MemoryActivityFeed(0)
    assert feed.latest == 0


# ----- chat auto-memory wiring -----


class Provider:
    name = "fake"
    model = "fake-model"

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(self.text, self.name, self.model)


GOOD = json.dumps(
    {"items": [{"fact": FACT, "kind": "routine", "quote": QUOTE}]}, ensure_ascii=False
)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database(tmp_path / "feed.sqlite3")
        self.database.initialize()
        self.memory = MemoryRepository(self.database)
        (tmp_path / "vault").mkdir()
        self.writer = MemoryWriter(self.memory, ObsidianVault(tmp_path / "vault"))
        self.feed = MemoryActivityFeed()
        self.research = ResearchRepository(self.database)

    def chat(self, *, auto_approve: bool, publisher=None) -> ChatAutoMemory:
        return ChatAutoMemory(
            Provider(GOOD),
            self.memory,
            self.writer,
            auto_approve=auto_approve,
            publisher=self.feed.publish if publisher is None else publisher,
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_chat_staged_publishes_a_staged_event(world: World) -> None:
    outcome = asyncio.run(world.chat(auto_approve=False).process(uuid4(), MESSAGE))
    (event,) = world.feed.since(0)
    assert event["kind"] == "staged" and event["origin"] == "chat"
    assert event["memory_id"] == str(outcome.staged[0].record.id)
    assert event["summary"] == FACT


def test_chat_auto_approved_publishes_approved_not_staged(world: World) -> None:
    asyncio.run(world.chat(auto_approve=True).process(uuid4(), MESSAGE))
    (event,) = world.feed.since(0)
    assert event["kind"] == "approved" and event["origin"] == "chat"


def test_chat_publishes_nothing_when_nothing_is_stored(world: World) -> None:
    asyncio.run(world.chat(auto_approve=False).process(uuid4(), "短い"))
    assert world.feed.since(0) == []


def test_chat_forget_publishes_withdrawn(world: World) -> None:
    auto = world.chat(auto_approve=True)
    asyncio.run(auto.process(uuid4(), MESSAGE))
    outcome = asyncio.run(auto.process(uuid4(), "毎朝Pythonでスクリプトを書く習慣のことは忘れて"))
    assert outcome.forgotten == 1
    assert [event["kind"] for event in world.feed.since(0)] == ["approved", "withdrawn"]


def test_a_failing_publisher_does_not_break_chat_memory(world: World) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("boom")

    outcome = asyncio.run(
        world.chat(auto_approve=True, publisher=broken).process(uuid4(), MESSAGE)
    )
    assert outcome.failure is None and outcome.approved == 1
    assert world.memory.get(outcome.staged[0].record.id) is not None


# ----- research wiring -----


def research_session(world: World, *claims: str) -> UUID:
    session = world.research.create_session("How long is the cache?", ResearchLevel.STANDARD)
    world.research.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    source = world.research.add_source(
        session.id,
        url=GOOD_URL,
        final_url=GOOD_URL,
        retrieved_at=RETRIEVED,
        content_digest=hashlib.sha256(b"x").hexdigest(),
        title="Foo docs",
        source_type=SourceType.DOCS,
        evaluation=SourceEvaluation(authority=0.85),
    )
    for text in claims or ("Foo cache TTL is 60 seconds.",):
        world.research.add_claim(
            session.id, claim_text=text, source_id=source.id, quote=CLAIM_QUOTE
        )
    world.research.set_result(session.id, "done")
    return session.id


def candidates(world: World, *, approve: bool, publisher=None) -> ResearchMemoryCandidates:
    return ResearchMemoryCandidates(
        world.research,
        world.memory,
        approve=(lambda i: world.writer.approve(i, actor=AUTO_APPROVER)) if approve else None,
        publisher=world.feed.publish if publisher is None else publisher,
    )


def test_research_staging_publishes_staged_once(world: World) -> None:
    service = candidates(world, approve=False)
    session_id = research_session(world, "Foo cache TTL is 60 seconds.")
    service.stage(session_id)
    (event,) = world.feed.since(0)
    assert event["kind"] == "staged" and event["origin"] == "research"
    assert event["summary"] == "Foo cache TTL is 60 seconds."
    service.stage(session_id)  # a repeat changes nothing, so announces nothing
    assert len(world.feed.since(0)) == 1


def test_research_enabled_approval_publishes_approved(world: World) -> None:
    candidates(world, approve=True).stage(research_session(world))
    (event,) = world.feed.since(0)
    assert event["kind"] == "approved" and event["origin"] == "research"


def test_research_failing_publisher_is_isolated(world: World) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("boom")

    result = candidates(world, approve=True, publisher=broken).stage(research_session(world))
    assert result.created == 1 and result.auto_approved == 1


def test_withdraw_route_publishes_withdrawn_for_both_origins(world: World) -> None:
    service = candidates(world, approve=True)
    service.stage(research_session(world))
    asyncio.run(world.chat(auto_approve=True).process(uuid4(), MESSAGE))
    app = FastAPI()
    app.include_router(
        create_memory_withdraw_router(world.memory, world.writer, publisher=world.feed.publish)
    )
    client = TestClient(app)
    approved = [e for e in world.feed.since(0) if e["kind"] == "approved"]
    assert {e["origin"] for e in approved} == {"research", "chat"}
    for event in approved:
        response = client.post(
            f"/api/memory/notes/{event['memory_id']}/withdraw", headers=HEADERS
        )
        assert response.status_code == 200
    withdrawn = [e for e in world.feed.since(0) if e["kind"] == "withdrawn"]
    assert {(e["origin"], e["memory_id"]) for e in withdrawn} == {
        (e["origin"], e["memory_id"]) for e in approved
    }
    # A refused withdrawal announces nothing.
    before = world.feed.latest
    client.post(f"/api/memory/notes/{uuid4()}/withdraw", headers=HEADERS)
    assert world.feed.latest == before


def test_manual_button_staging_publishes_through_the_router(world: World) -> None:
    app = FastAPI()
    app.include_router(create_research_memory_router(candidates(world, approve=False)))
    response = TestClient(app).post(
        f"/api/research/sessions/{research_session(world)}/memory-candidates", headers=HEADERS
    )
    assert response.status_code == 201
    assert [e["kind"] for e in world.feed.since(0)] == ["staged"]


# ----- endpoint -----


def endpoint(feed: MemoryActivityFeed, **flags: bool) -> TestClient:
    options = {"configured": True, "chat_enabled": True, "research_enabled": False} | flags
    app = FastAPI()
    app.include_router(create_memory_activity_router(feed, **options))
    app.include_router(create_memory_router(MemoryRepository(Database(Path(":memory:")))))
    return TestClient(app)


def test_endpoint_returns_events_after_the_cursor_without_caching() -> None:
    feed = MemoryActivityFeed()
    client = endpoint(feed)
    empty = client.get("/api/memory/activity")
    assert empty.status_code == 200
    assert empty.json() == {
        "latest": 0,
        "events": [],
        "configured": True,
        "chat_enabled": True,
        "research_enabled": False,
    }
    assert empty.headers["cache-control"] == "no-store"
    for text in ("a", "b", "c"):
        feed.publish(MemoryEventKind.STAGED, MemoryEventOrigin.CHAT, new_id(), text)
    body = client.get("/api/memory/activity", params={"after": "1"}).json()
    assert body["latest"] == 3 and [e["summary"] for e in body["events"]] == ["b", "c"]
    assert client.get("/api/memory/activity?after=3").json()["events"] == []
    assert client.get("/api/memory/activity?after=0").json()["latest"] == 3


def test_endpoint_reports_an_unconfigured_memory() -> None:
    body = endpoint(MemoryActivityFeed(), configured=False, chat_enabled=False).get(
        "/api/memory/activity"
    ).json()
    assert body["configured"] is False and body["chat_enabled"] is False


@pytest.mark.parametrize(
    "after", ["-1", "abc", "1.5", "", " 1", "1 ", "0x10", "١٢", "1" * 16, "1;2", "NaN"]
)
def test_endpoint_rejects_a_bad_cursor_with_a_fixed_code(after: str) -> None:
    feed = MemoryActivityFeed()
    feed.publish(MemoryEventKind.STAGED, MemoryEventOrigin.CHAT, new_id(), "x")
    response = endpoint(feed).get("/api/memory/activity", params={"after": after})
    assert response.status_code == 422 and response.json() == {"detail": "invalid_after"}


def test_endpoint_is_read_only() -> None:
    client = endpoint(MemoryActivityFeed())
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)("/api/memory/activity").status_code == 405


# ----- the whole app -----


def test_app_feed_shows_a_chat_memory_after_the_reply_and_flags(tmp_path: Path) -> None:
    from test_chat_auto_memory import GOOD as EXTRACTION
    from test_chat_auto_memory import ScriptedProvider, wait_for

    from backend.api.app import create_app
    from backend.core.config import Settings

    (tmp_path / "vault").mkdir()
    settings = Settings(
        db_path=tmp_path / "app.sqlite3",
        memory_vault_path=tmp_path / "vault",
        chat_memory_auto=True,
        chat_memory_auto_approve=True,
    )
    with TestClient(create_app(settings, ScriptedProvider(EXTRACTION))) as client:
        start = client.get("/api/memory/activity").json()
        assert start["latest"] == 0 and start["configured"] is True
        assert start["chat_enabled"] is True and start["research_enabled"] is False
        client.post("/api/chat", json={"message": MESSAGE})
        assert wait_for(lambda: client.get("/api/memory/activity").json()["events"])
        body = client.get("/api/memory/activity", params={"after": 0}).json()
        (event,) = body["events"]
        assert event["kind"] == "approved" and event["origin"] == "chat"
        assert event["summary"] == FACT
        assert client.get(f"/api/memory/activity?after={body['latest']}").json()["events"] == []
        withdrawn = client.post(
            f"/api/memory/notes/{event['memory_id']}/withdraw", headers=HEADERS
        )
        assert withdrawn.status_code == 200
        last = client.get(f"/api/memory/activity?after={body['latest']}").json()["events"]
        assert [e["kind"] for e in last] == ["withdrawn"]


def test_app_without_a_vault_reports_memory_unconfigured(tmp_path: Path) -> None:
    from test_chat_auto_memory import ScriptedProvider

    from backend.api.app import create_app
    from backend.core.config import Settings

    with TestClient(
        create_app(Settings(db_path=tmp_path / "app.sqlite3"), ScriptedProvider())
    ) as client:
        body = client.get("/api/memory/activity").json()
    assert body["configured"] is False and body["chat_enabled"] is False
