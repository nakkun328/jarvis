"""Conversation history: list, full transcript paging, titles and continuing old chats."""

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.chat.context import TITLE_LENGTH, UNTITLED, derive_title
from backend.core.config import Settings
from backend.providers.base import CompletionRequest, CompletionResponse


class Provider:
    name = "fake"
    model = "fake"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse("reply", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        yield "reply"


def make_client(tmp_path: Path, provider: Provider | None = None) -> TestClient:
    return TestClient(create_app(Settings(db_path=tmp_path / "h.sqlite3"), provider or Provider()))


def chat(client: TestClient, message: str, conversation_id: str | None = None) -> str:
    body: dict[str, str] = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = client.post("/api/chat", json=body)
    assert response.status_code == 200
    return response.json()["conversation_id"]


def test_list_is_newest_activity_first_with_titles_and_counts(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = chat(client, "first topic\nsecond line")
        second = chat(client, "other topic")
        chat(client, "back to first", first)
        response = client.get("/api/chat/conversations")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [c["id"] for c in body["conversations"]] == [first, second]
    assert body["conversations"][0]["title"] == "first topic"
    assert body["conversations"][0]["message_count"] == 4
    assert body["conversations"][1]["message_count"] == 2
    assert set(body["conversations"][0]) == {"id", "title", "updated_at", "message_count"}
    assert body["next_cursor"] is None
    assert "back to first" not in response.text and "reply" not in response.text


def test_list_pages_with_a_cursor_and_no_overlap(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ids = [chat(client, f"topic {i}") for i in range(5)]
        seen: list[str] = []
        url = "/api/chat/conversations?limit=2"
        pages = 0
        while True:
            body = client.get(url).json()
            pages += 1
            seen += [c["id"] for c in body["conversations"]]
            if body["next_cursor"] is None:
                break
            url = f"/api/chat/conversations?limit=2&before={body['next_cursor']}"
    assert pages == 3
    assert seen == list(reversed(ids))


def test_list_cursor_survives_equal_timestamps(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for i in range(4):
            chat(client, f"t{i}")
        with sqlite3.connect(tmp_path / "h.sqlite3") as connection:
            connection.execute("UPDATE conversations SET updated_at = '2026-01-01T00:00:00.000Z'")
        seen: list[str] = []
        url = "/api/chat/conversations?limit=1"
        while True:
            body = client.get(url).json()
            seen += [c["id"] for c in body["conversations"]]
            if body["next_cursor"] is None:
                break
            url = f"/api/chat/conversations?limit=1&before={body['next_cursor']}"
    assert len(seen) == 4 and len(set(seen)) == 4


def test_empty_list(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        body = client.get("/api/chat/conversations").json()
    assert body == {"conversations": [], "next_cursor": None}


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("limit=0", "invalid_limit"),
        ("limit=101", "invalid_limit"),
        ("limit=-1", "invalid_limit"),
        ("limit=abc", "invalid_limit"),
        ("limit=", "invalid_limit"),
        ("limit=1.5", "invalid_limit"),
        ("limit=%EF%BC%95", "invalid_limit"),
        ("limit=5&limit=6", "invalid_limit"),
        ("before=garbage", "invalid_before"),
        ("before=2026-01-01T00:00:00.000Z_not-a-uuid", "invalid_before"),
        ("before=2026-01-01T00:00:00.000Z_0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D", "invalid_before"),
        ("before=", "invalid_before"),
    ],
)
def test_list_validation_uses_fixed_codes(tmp_path: Path, query: str, code: str) -> None:
    with make_client(tmp_path) as client:
        response = client.get(f"/api/chat/conversations?{query}")
    assert response.status_code == 422
    assert response.json() == {"detail": code}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", "hello"),
        ("\n\n  second\nthird", "second"),
        ("a‮b​c\x00d\x1b[31m", "abcd[31m"),
        ("tab\there  and   spaces", "tab here and spaces"),
        ("x" * 100, "x" * TITLE_LENGTH),
        ("あ" * 100, "あ" * TITLE_LENGTH),
        ("", UNTITLED),
        ("‮​\x00", UNTITLED),
        ("   \n   ", UNTITLED),
        ("line next", "line"),
        ("<script>alert(1)</script>", "<script>alert(1)</script>"),
    ],
)
def test_title_derivation(text: str, expected: str) -> None:
    assert derive_title(text) == expected


def test_hostile_title_is_clean_in_the_list(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        chat(client, "‮evil⁦\x07 title\nsecret body line")
        body = client.get("/api/chat/conversations").json()
    assert body["conversations"][0]["title"] == "evil title"
    assert "secret body" not in str(body)


def test_full_transcript_pages_back_with_before(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        cid = None
        for i in range(15):
            cid = chat(client, f"m{i}", cid)
        url = f"/api/chat/conversations/{cid}/messages"
        newest = client.get(f"{url}?limit=10")
        assert newest.headers["cache-control"] == "no-store"
        page = newest.json()
        assert [m["content"] for m in page["messages"]][-2:] == ["m14", "reply"]
        assert len(page["messages"]) == 10 and page["has_more"] is True
        collected = list(page["messages"])
        while page["has_more"]:
            page = client.get(f"{url}?limit=10&before={page['next_before']}").json()
            collected = page["messages"] + collected
        assert len(collected) == 30
        assert collected[0] == {"role": "user", "content": "m0"}
        assert page["next_before"] is None
        past = client.get(f"{url}?before=1").json()
        assert past["messages"] == [] and past["has_more"] is False
        assert len(client.get(url).json()["messages"]) == 30


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=x", "limit=1&limit=2", "before=0", "before=x", "before=-3",
     "before=1234567890123", "before="],
)
def test_messages_validation_uses_fixed_codes(tmp_path: Path, query: str) -> None:
    with make_client(tmp_path) as client:
        cid = chat(client, "hi")
        response = client.get(f"/api/chat/conversations/{cid}/messages?{query}")
    assert response.status_code == 422
    assert response.json()["detail"] in {"invalid_limit", "invalid_before"}


def test_messages_limit_cap_is_200(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        cid = chat(client, "hi")
        assert client.get(f"/api/chat/conversations/{cid}/messages?limit=200").status_code == 200


def test_old_conversation_can_be_continued_after_restart_with_bounded_context(
    tmp_path: Path,
) -> None:
    provider = Provider()
    with make_client(tmp_path, provider) as client:
        cid = None
        for i in range(14):
            cid = chat(client, f"q{i}", cid)
    with make_client(tmp_path, provider) as client:
        chat(client, "continue", cid)
        stored = client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"]
    sent = provider.requests[-1].messages[1:]
    assert len(sent) == 21  # 20 retained messages plus the new one
    assert sent[-1].content == "continue"
    assert len(stored) == 30  # the transcript on disk is not truncated


def test_evicted_conversation_is_still_continuable_and_listed(tmp_path: Path) -> None:
    from backend.chat.persistence import SQLiteConversationStore
    from backend.chat.service import ChatService
    from backend.core.database import Database

    database = Database(tmp_path / "e.sqlite3")
    database.initialize()
    provider = Provider()
    store = SQLiteConversationStore(database, max_conversations=2)
    service = ChatService(provider, store=store)
    from fastapi import FastAPI

    from backend.api.chat import build_chat_router
    from backend.api.chat_history import build_chat_history_router

    app = FastAPI()
    app.include_router(build_chat_router(service))
    app.include_router(build_chat_history_router(service))
    with TestClient(app) as client:
        first = chat(client, "oldest")
        chat(client, "b")
        chat(client, "c")
        assert first not in {str(k) for k in store._conversations}
        chat(client, "back again", first)
        listing = client.get("/api/chat/conversations").json()["conversations"]
    assert [c["id"] for c in listing][0] == first
    assert len(listing) == 3
    assert [m.content for m in provider.requests[-1].messages[1:]] == [
        "oldest", "reply", "back again"
    ]


def test_in_memory_store_lists_and_pages() -> None:
    from fastapi import FastAPI

    from backend.api.chat import build_chat_router
    from backend.api.chat_history import build_chat_history_router
    from backend.chat.service import ChatService

    service = ChatService(Provider())
    app = FastAPI()
    app.include_router(build_chat_router(service))
    app.include_router(build_chat_history_router(service))
    with TestClient(app) as client:
        a = chat(client, "alpha")
        b = chat(client, "beta")
        listing = client.get("/api/chat/conversations?limit=1").json()
        assert [c["title"] for c in listing["conversations"]] == ["beta"]
        more = client.get(f"/api/chat/conversations?limit=1&before={listing['next_cursor']}")
        assert [c["id"] for c in more.json()["conversations"]] == [a]
        cid = b
        for i in range(12):
            cid = chat(client, f"m{i}", cid)
        page = client.get(f"/api/chat/conversations/{b}/messages?limit=10").json()
        older = client.get(f"/api/chat/conversations/{b}/messages?before={page['next_before']}")
    assert len(page["messages"]) == 10 and page["has_more"] is True
    assert older.status_code == 200
    # The process-local store keeps only its 20-message window (SQLite keeps everything).
    assert len(older.json()["messages"]) == 10 and older.json()["has_more"] is False


def test_unknown_conversation_messages_is_404_and_storage_errors_are_503(
    tmp_path: Path,
) -> None:
    with make_client(tmp_path) as client:
        unknown = "00000000-0000-4000-8000-000000000000"
        response = client.get(f"/api/chat/conversations/{unknown}/messages")
        assert response.status_code == 404
        (tmp_path / "h.sqlite3").unlink()
        assert client.get("/api/chat/conversations").status_code == 503
