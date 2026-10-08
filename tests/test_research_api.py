"""Read-only research API: list and detail views over stored research sessions."""

import ast
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.research as research_module
from backend.api.app import create_app
from backend.api.research import MAX_LIST_LIMIT, create_research_router
from backend.core.config import Settings
from backend.core.database import Database
from backend.research.models import (
    FailureReason,
    ResearchLevel,
    ResearchStatus,
    SourceEvaluation,
    SourceType,
)
from backend.research.repository import ResearchRepository, ResearchRepositoryError

START = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
INJECTION = (
    '<script>window.__pwned = 1</script><img src=x onerror="alert(1)">\n'
    'event: done\ndata: {"hacked": true}\n\n[x](javascript:alert(1))   ' + "A" * 300
)
SOURCE_KEYS = {
    "id", "url", "final_url", "title", "publisher", "published_at", "retrieved_at",
    "source_type", "evaluation",
}
SUMMARY_KEYS = {
    "id", "question", "level", "status", "failure_reason", "has_result", "created_at",
    "updated_at",
}


class Clock:
    """A clock that moves forward on every call, so list order is deterministic."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "research-api.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def repo(database: Database) -> ResearchRepository:
    return ResearchRepository(database, clock=Clock())


def build_client(repository: ResearchRepository) -> TestClient:
    app = FastAPI()
    app.include_router(create_research_router(repository))
    return TestClient(app)


@pytest.fixture
def client(repo: ResearchRepository) -> TestClient:
    return build_client(repo)


def add_source(repo: ResearchRepository, session_id, name="a", **overrides):
    values = {
        "url": f"https://example.test/{name}",
        "final_url": f"https://example.test/{name}",
        "retrieved_at": START,
        "content_digest": hashlib.sha256(name.encode()).hexdigest(),
    }
    values.update(overrides)
    return repo.add_source(session_id, **values)


def completed_session(repo: ResearchRepository, question="What is the answer?"):
    session = repo.create_session(question, ResearchLevel.STANDARD)
    repo.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repo.add_query(session.id, "first query")
    repo.add_query(session.id, "second query")
    source = add_source(
        repo,
        session.id,
        title="A title",
        publisher="A publisher",
        published_at=START - timedelta(days=3),
        source_type=SourceType.DOCS,
        evaluation=SourceEvaluation(authority=0.9, freshness=0.5, primary=1.0, relevance=0.75),
    )
    repo.add_claim(
        session.id,
        claim_text="The answer is stored.",
        source_id=source.id,
        quote="the answer is stored",
        quote_start=10,
        quote_end=30,
    )
    repo.set_result(session.id, "Result text.")
    return source, repo.get_session(session.id)


# ----- list -----


def test_list_is_empty_without_sessions(client: TestClient) -> None:
    response = client.get("/api/research/sessions")
    assert response.status_code == 200
    assert response.json() == {"sessions": []}


def test_list_returns_allowlisted_summaries_newest_first(
    client: TestClient, repo: ResearchRepository
) -> None:
    first = repo.create_session("first?")
    second = repo.create_session("second?", ResearchLevel.DEEP)
    repo.transition(second.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repo.transition(
        second.id, ResearchStatus.RUNNING, ResearchStatus.FAILED,
        failure_reason=FailureReason.NO_RESULTS,
    )
    body = client.get("/api/research/sessions").json()
    assert [item["id"] for item in body["sessions"]] == [str(second.id), str(first.id)]
    for item in body["sessions"]:
        assert set(item) == SUMMARY_KEYS
    assert body["sessions"][1] == {
        "id": str(first.id),
        "question": "first?",
        "level": "quick",
        "status": "pending",
        "failure_reason": None,
        "has_result": False,
        "created_at": "2026-10-07T12:00:01.000000Z",
        "updated_at": "2026-10-07T12:00:01.000000Z",
    }
    assert body["sessions"][0]["level"] == "deep"
    assert body["sessions"][0]["status"] == "failed"
    assert body["sessions"][0]["failure_reason"] == "no_results"


def test_list_with_limit_keeps_the_newest_sessions(
    client: TestClient, repo: ResearchRepository
) -> None:
    ids = [str(repo.create_session(f"q{n}?").id) for n in range(5)]
    body = client.get("/api/research/sessions", params={"limit": 2}).json()
    assert [item["id"] for item in body["sessions"]] == ids[::-1][:2]
    everything = client.get("/api/research/sessions").json()
    assert [item["id"] for item in everything["sessions"]] == ids[::-1]


def test_list_summary_has_no_result_text_or_children(
    client: TestClient, repo: ResearchRepository
) -> None:
    completed_session(repo)
    item = client.get("/api/research/sessions").json()["sessions"][0]
    assert item["has_result"] is True
    assert "Result text." not in json.dumps(item)
    for key in ("result_text", "queries", "sources", "claims"):
        assert key not in item


def test_list_status_filter(client: TestClient, repo: ResearchRepository) -> None:
    pending = repo.create_session("pending?")
    running = repo.create_session("running?")
    repo.transition(running.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    got = client.get("/api/research/sessions", params={"status": "running"}).json()
    assert [item["id"] for item in got["sessions"]] == [str(running.id)]
    got = client.get("/api/research/sessions", params={"status": "pending"}).json()
    assert [item["id"] for item in got["sessions"]] == [str(pending.id)]
    got = client.get("/api/research/sessions", params={"status": "completed"}).json()
    assert got == {"sessions": []}


def test_list_limit_bounds(client: TestClient, repo: ResearchRepository) -> None:
    for number in range(5):
        repo.create_session(f"question {number}?")
    assert len(client.get("/api/research/sessions").json()["sessions"]) == 5
    assert len(client.get("/api/research/sessions?limit=2").json()["sessions"]) == 2
    assert len(client.get("/api/research/sessions?limit=1").json()["sessions"]) == 1
    assert client.get(f"/api/research/sessions?limit={MAX_LIST_LIMIT}").status_code == 200
    assert [
        item["question"] for item in client.get("/api/research/sessions?limit=2").json()["sessions"]
    ] == ["question 4?", "question 3?"]


@pytest.mark.parametrize(
    "limit", ["0", "-1", "101", "1000000", "abc", "1.5", "", " 5", "5 ", "%EF%BC%95", "1e1", "+5"]
)
def test_list_rejects_bad_limits_with_a_fixed_code(client: TestClient, limit: str) -> None:
    response = client.get(f"/api/research/sessions?limit={limit}")
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_limit"}


@pytest.mark.parametrize(
    "status", ["bogus", "RUNNING", "", "running%20", "all", "<script>alert(1)</script>"]
)
def test_list_rejects_bad_status_without_echoing_it(client: TestClient, status: str) -> None:
    response = client.get(f"/api/research/sessions?status={status}")
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_status"}
    assert "script" not in response.text and "bogus" not in response.text


# ----- detail -----


def test_detail_returns_session_queries_sources_and_claims(
    client: TestClient, repo: ResearchRepository
) -> None:
    source, session = completed_session(repo)
    response = client.get(f"/api/research/sessions/{session.id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == SUMMARY_KEYS | {"result_text", "queries", "sources", "claims"}
    assert body["id"] == str(session.id)
    assert body["status"] == "completed"
    assert body["level"] == "standard"
    assert body["result_text"] == "Result text."
    assert body["has_result"] is True
    assert body["failure_reason"] is None
    assert [(q["position"], q["text"]) for q in body["queries"]] == [
        (0, "first query"),
        (1, "second query"),
    ]
    assert set(body["queries"][0]) == {"position", "text", "created_at"}
    [source_body] = body["sources"]
    assert set(source_body) == SOURCE_KEYS
    assert source_body["id"] == str(source.id)
    assert source_body["title"] == "A title"
    assert source_body["publisher"] == "A publisher"
    assert source_body["url"] == "https://example.test/a"
    assert source_body["final_url"] == "https://example.test/a"
    assert source_body["source_type"] == "docs"
    assert source_body["published_at"] == "2026-10-04T12:00:00.000000Z"
    assert source_body["retrieved_at"] == "2026-10-07T12:00:00.000000Z"
    assert source_body["evaluation"] == {
        "authority": 0.9,
        "freshness": 0.5,
        "primary": 1.0,
        "relevance": 0.75,
        "agreement": None,
    }
    [claim] = body["claims"]
    assert set(claim) == {
        "id", "claim_text", "source_id", "quote", "quote_start", "quote_end"
    }
    assert claim["source_id"] == str(source.id)
    assert claim["claim_text"] == "The answer is stored."
    assert claim["quote"] == "the answer is stored"
    assert (claim["quote_start"], claim["quote_end"]) == (10, 30)


def test_detail_never_exposes_the_content_digest(
    client: TestClient, repo: ResearchRepository
) -> None:
    _, session = completed_session(repo)
    digest = hashlib.sha256(b"a").hexdigest()
    assert digest not in client.get(f"/api/research/sessions/{session.id}").text


def test_detail_of_a_failed_session_carries_only_the_fixed_reason(
    client: TestClient, repo: ResearchRepository
) -> None:
    session = repo.create_session("will fail?")
    repo.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repo.transition(
        session.id, ResearchStatus.RUNNING, ResearchStatus.FAILED,
        failure_reason=FailureReason.TIMEOUT,
    )
    body = client.get(f"/api/research/sessions/{session.id}").json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "timeout"
    assert body["result_text"] is None
    assert (body["queries"], body["sources"], body["claims"]) == ([], [], [])


def test_detail_of_a_pending_session_has_no_children(
    client: TestClient, repo: ResearchRepository
) -> None:
    session = repo.create_session("not started?")
    body = client.get(f"/api/research/sessions/{session.id}").json()
    assert body["status"] == "pending"
    assert body["has_result"] is False
    assert body["result_text"] is None


# ----- not found -----


def test_unknown_session_is_404_with_a_fixed_body(client: TestClient) -> None:
    response = client.get(f"/api/research/sessions/{uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"detail": "session_not_found"}


@pytest.mark.parametrize(
    "session_id",
    [
        "not-a-uuid",
        "123",
        "00000000000000000000000000000000",
        "{11111111-1111-4111-8111-111111111111}",
        "urn:uuid:11111111-1111-4111-8111-111111111111",
        "11111111-1111-4111-8111-11111111111G",
        "%3Cscript%3Ealert(1)%3Cscript%3E",
        "A" * 5000,
    ],
)
def test_invalid_ids_are_404_without_echo(client: TestClient, session_id: str) -> None:
    response = client.get(f"/api/research/sessions/{session_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "session_not_found"}


@pytest.mark.parametrize("session_id", ["%3Cscript%3E%2Fx", "..%2F..%2Fetc%2Fpasswd"])
def test_ids_with_slashes_match_no_route_and_are_not_echoed(
    client: TestClient, session_id: str
) -> None:
    response = client.get(f"/api/research/sessions/{session_id}")
    assert response.status_code == 404
    assert "script" not in response.text and "passwd" not in response.text


def test_non_canonical_uuid_spelling_is_not_found(
    client: TestClient, repo: ResearchRepository
) -> None:
    session = repo.create_session("canonical?")
    assert client.get(f"/api/research/sessions/{session.id}").status_code == 200
    assert client.get(f"/api/research/sessions/{str(session.id).upper()}").status_code == 404
    assert client.get(f"/api/research/sessions/{session.id.hex}").status_code == 404


# ----- untrusted text, errors, and read-only behaviour -----


def test_injection_like_stored_text_is_only_ever_json_strings(
    client: TestClient, repo: ResearchRepository
) -> None:
    session = repo.create_session(INJECTION[:1900])
    repo.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    repo.add_query(session.id, INJECTION[:400])
    source = add_source(repo, session.id, title=INJECTION[:400], publisher=INJECTION[:150])
    repo.add_claim(
        session.id, claim_text=INJECTION[:1900], source_id=source.id, quote=INJECTION[:400]
    )
    repo.set_result(session.id, INJECTION)
    listing = client.get("/api/research/sessions")
    detail = client.get(f"/api/research/sessions/{session.id}")
    for response in (listing, detail):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "text/html" not in response.headers["content-type"]
        assert response.headers.get("x-content-type-options", "nosniff") == "nosniff"
    body = detail.json()
    assert body["question"] == INJECTION[:1900]
    assert body["result_text"] == INJECTION
    assert body["queries"][0]["text"] == INJECTION[:400]
    assert body["sources"][0]["title"] == INJECTION[:400]
    assert body["claims"][0]["quote"] == INJECTION[:400]
    assert set(body) == SUMMARY_KEYS | {"result_text", "queries", "sources", "claims"}
    # The hostile text did not become structure: the id fields are still plain UUIDs.
    assert re.fullmatch(r"[0-9a-f-]{36}", body["id"])
    assert listing.json()["sessions"][0]["question"] == INJECTION[:1900]


class BrokenRepository(ResearchRepository):
    def __init__(self, database: Database, message: str) -> None:
        super().__init__(database)
        self.message = message

    def list_sessions(self, status=None, *, limit=100, newest_first=False):
        raise ResearchRepositoryError(self.message)

    def get_session(self, session_id):
        raise ResearchRepositoryError(self.message)


def test_storage_errors_are_503_with_a_fixed_body(database: Database) -> None:
    leaky = "disk path leaked-detail abc"
    client = build_client(BrokenRepository(database, leaky))
    for path in ("/api/research/sessions", f"/api/research/sessions/{uuid4()}"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": "storage_unavailable"}
        assert "disk path" not in response.text and "abc" not in response.text


def test_later_read_failure_in_detail_is_also_503(
    database: Database, repo: ResearchRepository
) -> None:
    session = repo.create_session("fails midway?")

    class FailsOnSources(ResearchRepository):
        def list_sources(self, session_id):
            raise ResearchRepositoryError("boom")

    client = build_client(FailsOnSources(database))
    response = client.get(f"/api/research/sessions/{session.id}")
    assert response.status_code == 503
    assert response.json() == {"detail": "storage_unavailable"}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path", ["/api/research/sessions", f"/api/research/sessions/{uuid4()}"]
)
def test_there_are_no_write_routes(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == 405


def test_reading_changes_nothing(client: TestClient, repo: ResearchRepository) -> None:
    _, session = completed_session(repo)
    before = repo.get_session(session.id)
    client.get("/api/research/sessions")
    client.get(f"/api/research/sessions/{session.id}")
    assert repo.get_session(session.id) == before
    assert len(repo.list_sessions()) == 1


def test_router_module_only_depends_on_stored_research_data() -> None:
    tree = ast.parse(Path(research_module.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert {name for name in imported if name.startswith("backend.")} == {
        "backend.research.models",
        "backend.research.repository",
    }
    assert not imported & {"httpx", "requests", "urllib.request", "socket", "aiohttp"}


def test_responses_contain_no_environment_secrets(
    client: TestClient, repo: ResearchRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "dummy-" + uuid4().hex
    monkeypatch.setenv("JARVIS_TEST_ONLY_TOKEN", token)
    _, session = completed_session(repo)
    texts = [
        client.get("/api/research/sessions").text,
        client.get(f"/api/research/sessions/{session.id}").text,
        client.get(f"/api/research/sessions/{uuid4()}").text,
    ]
    assert all(token not in text for text in texts)
    assert all("sqlite" not in text.lower() for text in texts)


# ----- app wiring -----


def test_app_serves_the_research_api(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "app.sqlite3")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/research/sessions").json() == {"sessions": []}
        assert client.get(f"/api/research/sessions/{uuid4()}").status_code == 404
        assert client.get("/health/live").status_code == 200
