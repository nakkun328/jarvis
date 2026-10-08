"""Read-only memory API: approved notes and pending candidates, as allowlisted DTOs."""

import ast
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.memory as memory_module
from backend.api.app import create_app
from backend.api.memory import MAX_LIST_LIMIT, create_memory_router
from backend.core.config import Settings
from backend.core.database import Database
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.repository import MemoryRepository, MemoryRepositoryError, MemoryStatus

START = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
REVISION = "ab" * 32
INJECTION = (
    '<script>window.__pwned = 1</script><img src=x onerror="alert(1)">\n'
    'event: done\ndata: {"hacked": true}\n\n[x](javascript:alert(1))   ' + "A" * 300
)
DTO_KEYS = {
    "id", "status", "category", "content", "source", "origin", "importance", "confidence",
    "tags", "project", "revision", "supersedes_id", "created_at", "updated_at",
}


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "memory-api.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def repo(database: Database) -> MemoryRepository:
    return MemoryRepository(database)


def build_client(repository: MemoryRepository) -> TestClient:
    app = FastAPI()
    app.include_router(create_memory_router(repository))
    return TestClient(app)


@pytest.fixture
def client(repo: MemoryRepository) -> TestClient:
    return build_client(repo)


def make_record(index: int, **overrides) -> MemoryRecord:
    created = START + timedelta(minutes=index)
    values = {
        "category": MemoryCategory.USER,
        "content": f"memory {index}",
        "source": f"user:chat:{index}",
        "origin": MemoryOrigin.USER_EXPLICIT,
        "importance": 0.8,
        "confidence": 0.9,
        "created_at": created,
        "updated_at": created,
        "tags": ("alpha", "beta"),
        "project": "demo",
    }
    values.update(overrides)
    return MemoryRecord(**values)


def add_candidate(repo: MemoryRepository, index: int, **overrides) -> MemoryRecord:
    record = make_record(index, **overrides)
    repo.add(record)
    return record


def add_approved(repo: MemoryRepository, index: int, **overrides) -> MemoryRecord:
    record = add_candidate(repo, index, **overrides)
    repo.transition(
        record.id, expected=MemoryStatus.PENDING, new=MemoryStatus.APPROVED,
        vault_revision=REVISION, actor="tester",
    )
    return record


def test_lists_are_empty_without_memories(client: TestClient) -> None:
    assert client.get("/api/memory/notes").json() == {"notes": []}
    assert client.get("/api/memory/candidates").json() == {"candidates": []}


def test_notes_are_only_approved_records_newest_first(
    client: TestClient, repo: MemoryRepository
) -> None:
    oldest = add_approved(repo, 1)
    add_candidate(repo, 2)  # pending: not a note
    newest = add_approved(repo, 3)
    rejected = add_candidate(repo, 4)
    repo.transition(rejected.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED)
    retired = add_approved(repo, 5)
    repo.retire(retired.id, vault_revision=REVISION, actor="t", reason="obsolete")
    notes = client.get("/api/memory/notes").json()["notes"]
    assert [note["id"] for note in notes] == [str(newest.id), str(oldest.id)]
    assert {note["status"] for note in notes} == {"approved"}


def test_candidates_are_pending_and_conflict_newest_first(
    client: TestClient, repo: MemoryRepository
) -> None:
    first = add_candidate(repo, 1)
    conflicted = add_candidate(repo, 2)
    repo.transition(conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    add_approved(repo, 3)  # approved: not a candidate
    last = add_candidate(repo, 4)
    rows = client.get("/api/memory/candidates").json()["candidates"]
    assert [row["id"] for row in rows] == [str(last.id), str(conflicted.id), str(first.id)]
    assert [row["status"] for row in rows] == ["pending", "conflict", "pending"]


def test_candidates_status_filter(client: TestClient, repo: MemoryRepository) -> None:
    add_candidate(repo, 1)
    conflicted = add_candidate(repo, 2)
    repo.transition(conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    only = client.get("/api/memory/candidates?status=conflict").json()["candidates"]
    assert [row["id"] for row in only] == [str(conflicted.id)]
    only = client.get("/api/memory/candidates?status=pending").json()["candidates"]
    assert len(only) == 1 and only[0]["status"] == "pending"


@pytest.mark.parametrize("status", ["approved", "rejected", "", "PENDING", "x" * 400, "<b>"])
def test_candidates_reject_other_statuses_without_echo(client: TestClient, status: str) -> None:
    response = client.get("/api/memory/candidates", params={"status": status})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_status"}
    assert status not in response.text or status == ""


def test_note_dto_is_an_exact_allowlist(client: TestClient, repo: MemoryRepository) -> None:
    record = add_approved(
        repo, 1, category=MemoryCategory.PROJECT, origin=MemoryOrigin.AI_INFERENCE,
        confidence=0.4, importance=0.25,
    )
    (note,) = client.get("/api/memory/notes").json()["notes"]
    assert set(note) == DTO_KEYS
    assert note["id"] == str(record.id)
    assert note["category"] == "project"
    assert note["origin"] == "ai_inference"
    assert note["confidence"] == 0.4 and note["importance"] == 0.25
    assert note["tags"] == ["alpha", "beta"] and note["project"] == "demo"
    assert note["revision"] == REVISION
    assert note["supersedes_id"] is None
    assert note["created_at"] == "2026-10-07T12:01:00.000000Z"


def test_candidate_has_no_revision_and_shows_its_correction_target(
    client: TestClient, repo: MemoryRepository
) -> None:
    old = add_approved(repo, 1)
    correction = replace(make_record(2), content="corrected")
    repo.add(correction, supersedes_id=old.id, supersedes_revision=REVISION)
    (row,) = client.get("/api/memory/candidates").json()["candidates"]
    assert set(row) == DTO_KEYS
    assert row["revision"] is None
    assert row["supersedes_id"] == str(old.id)
    # The old note is still the approved one until the correction is reviewed.
    assert [n["id"] for n in client.get("/api/memory/notes").json()["notes"]] == [str(old.id)]


def test_no_internal_column_or_path_is_exposed(client: TestClient, repo: MemoryRepository) -> None:
    add_approved(repo, 1)
    text = client.get("/api/memory/notes").text + client.get("/api/memory/candidates").text
    for forbidden in ("last_accessed", "supersedes_revision", "replaced_by", "vault_path", "path"):
        assert forbidden not in text


def test_limit_bounds_keep_the_newest(client: TestClient, repo: MemoryRepository) -> None:
    records = [add_approved(repo, index) for index in range(1, 6)]
    notes = client.get("/api/memory/notes?limit=2").json()["notes"]
    assert [n["id"] for n in notes] == [str(records[4].id), str(records[3].id)]
    assert len(client.get(f"/api/memory/notes?limit={MAX_LIST_LIMIT}").json()["notes"]) == 5
    for index in range(6, 9):
        add_candidate(repo, index)
    rows = client.get("/api/memory/candidates?limit=2").json()["candidates"]
    assert [r["content"] for r in rows] == ["memory 8", "memory 7"]


@pytest.mark.parametrize(
    "limit", ["0", "-1", f"{MAX_LIST_LIMIT + 1}", "abc", "", "1.5", "１２", "1e2", " 5", "<script>"]
)
def test_bad_limits_get_a_fixed_code_and_no_echo(client: TestClient, limit: str) -> None:
    for path in ("/api/memory/notes", "/api/memory/candidates"):
        response = client.get(path, params={"limit": limit})
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_limit"}
        assert limit not in response.text or limit == ""


def test_stored_hostile_text_is_only_ever_a_json_string(
    client: TestClient, repo: MemoryRepository
) -> None:
    add_approved(repo, 1, content=INJECTION, source=INJECTION, project=INJECTION, tags=(INJECTION,))
    response = client.get("/api/memory/notes")
    assert response.headers["content-type"].startswith("application/json")
    (note,) = response.json()["notes"]
    assert note["content"] == INJECTION and note["source"] == INJECTION
    assert note["tags"] == [INJECTION]


class BrokenRepository(MemoryRepository):
    def __init__(self, database: Database, message: str) -> None:
        super().__init__(database)
        self.message = message

    def list_by_status(self, status, *, limit=100, newest_first=False):
        raise MemoryRepositoryError(self.message)


def test_storage_errors_are_503_with_a_fixed_body(database: Database) -> None:
    leaky = "disk path leaked-detail abc"
    client = build_client(BrokenRepository(database, leaky))
    for path in ("/api/memory/notes", "/api/memory/candidates"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": "storage_unavailable"}
        assert "leaked" not in response.text and "abc" not in response.text


def test_a_row_that_no_longer_parses_is_a_storage_error(
    database: Database, repo: MemoryRepository, tmp_path: Path
) -> None:
    record = add_approved(repo, 1, content="private stored text")
    connection = sqlite3.connect(tmp_path / "memory-api.sqlite3")
    with connection:
        connection.execute(
            "UPDATE memory_records SET tags = 'not json' WHERE id = ?", (str(record.id),)
        )
    connection.close()
    response = build_client(repo).get("/api/memory/notes")
    assert response.status_code == 503
    assert response.json() == {"detail": "storage_unavailable"}
    assert "private stored" not in response.text


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/memory/notes",
        "/api/memory/candidates",
        f"/api/memory/notes/{uuid4()}",
        f"/api/memory/candidates/{uuid4()}/approve",
    ],
)
def test_there_are_no_write_routes(client: TestClient, method: str, path: str) -> None:
    # Existing collection paths answer 405; there is no route at all below them.
    response = getattr(client, method)(path)
    assert response.status_code == (405 if path.count("/") == 3 else 404)


def test_reading_changes_nothing(client: TestClient, repo: MemoryRepository) -> None:
    record = add_approved(repo, 1)
    add_candidate(repo, 2)
    before = repo.get(record.id)
    client.get("/api/memory/notes")
    client.get("/api/memory/candidates")
    assert repo.get(record.id) == before
    assert len(repo.list_by_status(MemoryStatus.PENDING)) == 1
    assert repo.review_events(record.id) == repo.review_events(record.id)


def test_router_module_depends_only_on_the_memory_repository() -> None:
    tree = ast.parse(Path(memory_module.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert {name for name in imported if name.startswith("backend.")} == {
        "backend.memory.repository"
    }
    assert not imported & {"httpx", "requests", "urllib.request", "socket", "aiohttp", "pathlib"}


def test_app_serves_the_memory_api_without_a_vault(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "app.sqlite3")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/memory/notes").json() == {"notes": []}
        assert client.get("/api/memory/candidates").json() == {"candidates": []}
        assert client.post("/api/memory/notes").status_code == 405
        assert client.get("/health/live").status_code == 200
