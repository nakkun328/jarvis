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
DETAIL_KEYS_EXTRA = {"replaced_by_id", "lifecycle", "reviews"}
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
    # Existing paths (collections and detail) answer 405; there is no route below them.
    response = getattr(client, method)(path)
    assert response.status_code == (404 if path.endswith("/approve") else 405)


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
        "backend.memory.repository",
        # Owner-approved research auto-approval (docs/memory.md): read-only audit lookups.
        "backend.memory.auto_approval",
        "backend.memory.model",
    }
    assert not imported & {"httpx", "requests", "urllib.request", "socket", "aiohttp", "pathlib"}


def test_app_serves_the_memory_api_without_a_vault(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "app.sqlite3")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/memory/notes").json() == {"notes": []}
        assert client.get("/api/memory/candidates").json() == {"candidates": []}
        assert client.post("/api/memory/notes").status_code == 405
        assert client.get("/health/live").status_code == 200


# ----- search -----


def ids(response) -> list[str]:
    body = response.json()
    (rows,) = body.values()
    return [row["id"] for row in rows]


def test_search_matches_content_project_category_and_tags_newest_first(
    client: TestClient, repo: MemoryRepository
) -> None:
    by_content = add_approved(repo, 1, content="Likes green TEA", tags=("x",), project="p1")
    by_project = add_approved(repo, 2, content="other", tags=("x",), project="Tea-room")
    by_tag = add_approved(repo, 3, content="other", tags=("brewing", "TEA"), project="p1")
    by_category = add_approved(
        repo, 4, content="other", category=MemoryCategory.WORK_STATE, tags=(), project=None
    )
    add_approved(repo, 5, content="nothing relevant", tags=("coffee",), project="p2")
    found = client.get("/api/memory/notes", params={"q": "tea"})
    assert ids(found) == [str(by_tag.id), str(by_project.id), str(by_content.id)]
    assert ids(client.get("/api/memory/notes", params={"q": "work_state"})) == [
        str(by_category.id)
    ]
    assert ids(client.get("/api/memory/notes", params={"q": "ブルー"})) == []


def test_search_terms_are_all_required_and_japanese_matches_exactly(
    client: TestClient, repo: MemoryRepository
) -> None:
    both = add_approved(repo, 1, content="緑茶が好き", tags=("飲み物",))
    add_approved(repo, 2, content="緑のペン", tags=("文具",))
    assert ids(client.get("/api/memory/notes", params={"q": "緑茶"})) == [str(both.id)]
    assert ids(client.get("/api/memory/notes", params={"q": "緑 飲み物"})) == [str(both.id)]
    # Ideographic space separates terms too; a blank query is "no search".
    assert ids(client.get("/api/memory/notes", params={"q": "緑\u3000飲み物"})) == [str(both.id)]
    assert len(ids(client.get("/api/memory/notes", params={"q": "   "}))) == 2
    assert len(ids(client.get("/api/memory/notes", params={"q": ""}))) == 2


def test_search_is_a_plain_substring_test_never_a_pattern(
    client: TestClient, repo: MemoryRepository
) -> None:
    plain = add_approved(repo, 1, content="100% sure", tags=("a_b",))
    add_approved(repo, 2, content="1000 sure", tags=("axb",))
    add_approved(repo, 3, content="back\\slash", tags=("t",))
    # LIKE wildcards have no meaning: "%" and "_" only match themselves.
    assert ids(client.get("/api/memory/notes", params={"q": "%"})) == [str(plain.id)]
    assert ids(client.get("/api/memory/notes", params={"q": "a_b"})) == [str(plain.id)]
    assert ids(client.get("/api/memory/notes", params={"q": "1%s"})) == []  # would match as LIKE
    assert len(ids(client.get("/api/memory/notes", params={"q": "\\"}))) == 1
    # SQL syntax is just text; the table is untouched.
    for attack in ("'", "' OR '1'='1", "x'; DROP TABLE memory_records; --", '"', "%' --"):
        response = client.get("/api/memory/notes", params={"q": attack})
        assert response.status_code == 200 and ids(response) == []
    assert len(ids(client.get("/api/memory/notes"))) == 3


def test_tag_json_syntax_is_not_searchable(client: TestClient, repo: MemoryRepository) -> None:
    add_approved(repo, 1, content="c", tags=("one", "two"), project=None)
    for syntax in ('"', "[", '","', "]"):
        assert ids(client.get("/api/memory/notes", params={"q": syntax})) == []


def test_search_candidates_covers_pending_and_conflict_and_the_status_filter(
    client: TestClient, repo: MemoryRepository
) -> None:
    pending = add_candidate(repo, 1, content="coffee order")
    flagged = add_candidate(repo, 2, content="coffee beans")
    repo.transition(flagged.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    add_candidate(repo, 3, content="tea")
    add_approved(repo, 4, content="coffee approved")
    rejected = add_candidate(repo, 5, content="coffee rejected")
    repo.transition(rejected.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED)
    response = client.get("/api/memory/candidates", params={"q": "coffee"})
    assert ids(response) == [str(flagged.id), str(pending.id)]
    narrowed = client.get("/api/memory/candidates", params={"q": "coffee", "status": "pending"})
    assert ids(narrowed) == [str(pending.id)]
    # Approved notes never appear in the candidate search and vice versa.
    assert len(ids(client.get("/api/memory/notes", params={"q": "coffee"}))) == 1


def test_search_respects_the_limit_and_keeps_the_newest(
    client: TestClient, repo: MemoryRepository
) -> None:
    records = [add_approved(repo, index, content=f"needle {index}") for index in range(1, 6)]
    response = client.get("/api/memory/notes", params={"q": "needle", "limit": "2"})
    assert ids(response) == [str(records[4].id), str(records[3].id)]
    assert client.get("/api/memory/notes", params={"q": "needle", "limit": "0"}).status_code == 422


@pytest.mark.parametrize(
    "query",
    [
        "x" * 101,
        "a\x00b",
        "a\nb",
        "a\tb",
        "a\rb",
        "a\x1bb",
        "a\x7fb",
        "a\x85b",
        "a\u2028b",
        "a\u2029b",
        "one two three four five six seven eight nine",
    ],
)
def test_bad_queries_get_a_fixed_code_and_are_never_echoed(
    client: TestClient, query: str
) -> None:
    for path in ("/api/memory/notes", "/api/memory/candidates"):
        response = client.get(path, params={"q": query})
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_query"}
        assert query not in response.text and "xxxxxxxx" not in response.text


def test_a_query_of_exactly_the_limit_is_accepted(client: TestClient) -> None:
    assert client.get("/api/memory/notes", params={"q": "x" * 100}).status_code == 200
    assert client.get("/api/memory/notes", params={"q": "語" * 100}).status_code == 200
    eight = " ".join("t" for _ in range(8))
    assert client.get("/api/memory/notes", params={"q": eight}).status_code == 200


def test_hostile_stored_text_is_found_and_still_only_json(
    client: TestClient, repo: MemoryRepository
) -> None:
    add_approved(repo, 1, content=INJECTION, tags=("<script>",))
    response = client.get("/api/memory/notes", params={"q": "<script>"})
    assert response.headers["content-type"].startswith("application/json")
    (note,) = response.json()["notes"]
    assert note["content"] == INJECTION


def test_search_storage_errors_are_503_with_a_fixed_body(database: Database) -> None:
    client = build_client(SearchBrokenRepository(database, "leaky path abc"))
    response = client.get("/api/memory/notes", params={"q": "x"})
    assert response.status_code == 503 and response.json() == {"detail": "storage_unavailable"}
    assert "leaky" not in response.text


class SearchBrokenRepository(MemoryRepository):
    def __init__(self, database: Database, message: str) -> None:
        super().__init__(database)
        self.message = message

    def search_by_status(self, statuses, terms, *, limit=50):
        raise MemoryRepositoryError(self.message)


def test_a_row_with_bad_tags_is_a_storage_error_when_searching(
    repo: MemoryRepository, tmp_path: Path
) -> None:
    record = add_approved(repo, 1, content="private stored text")
    connection = sqlite3.connect(tmp_path / "memory-api.sqlite3")
    with connection:
        connection.execute(
            "UPDATE memory_records SET tags = 'not json' WHERE id = ?", (str(record.id),)
        )
    connection.close()
    response = build_client(repo).get("/api/memory/notes", params={"q": "private"})
    assert response.status_code == 503
    assert "private stored" not in response.text


# ----- detail -----


def test_note_detail_is_an_allowlist_with_review_history(
    client: TestClient, repo: MemoryRepository
) -> None:
    record = add_approved(repo, 1, content="detail text")
    body = client.get(f"/api/memory/notes/{record.id}").json()
    assert set(body) == DTO_KEYS | DETAIL_KEYS_EXTRA
    assert body["id"] == str(record.id) and body["content"] == "detail text"
    assert body["status"] == "approved" and body["revision"] == REVISION
    assert body["replaced_by_id"] is None and body["lifecycle"] == []
    (review,) = body["reviews"]
    assert set(review) == {"action", "previous_status", "new_status", "occurred_at", "revision"}
    assert review["action"] == "approve" and review["revision"] == REVISION
    assert review["previous_status"] == "pending" and review["new_status"] == "approved"
    # The free-text actor ("tester") is a review-CLI detail and is not exposed.
    assert "tester" not in client.get(f"/api/memory/notes/{record.id}").text


def test_a_replaced_note_links_to_its_replacement_and_back(
    client: TestClient, repo: MemoryRepository
) -> None:
    old = add_approved(repo, 1, content="old text")
    correction = replace(make_record(2), content="new text")
    repo.add(correction, supersedes_id=old.id, supersedes_revision=REVISION)
    candidate = client.get(f"/api/memory/candidates/{correction.id}").json()
    assert candidate["supersedes_id"] == str(old.id) and candidate["revision"] is None
    assert client.get(f"/api/memory/notes/{correction.id}").status_code == 404
    repo.transition(
        correction.id, expected=MemoryStatus.PENDING, new=MemoryStatus.APPROVED,
        vault_revision="cd" * 32, actor="tester",
    )
    replaced = client.get(f"/api/memory/notes/{old.id}").json()
    assert replaced["status"] == "superseded"
    assert replaced["replaced_by_id"] == str(correction.id)
    (event,) = replaced["lifecycle"]
    assert set(event) == {"action", "related_id", "occurred_at", "revision"}
    assert event["action"] == "supersede" and event["related_id"] == str(correction.id)
    assert event["revision"] == REVISION
    current = client.get(f"/api/memory/notes/{correction.id}").json()
    assert current["supersedes_id"] == str(old.id) and current["status"] == "approved"
    # A replaced note is not in the approved list, but it stays readable by id.
    assert str(old.id) not in ids(client.get("/api/memory/notes"))


def test_a_retired_note_stays_readable_by_id(client: TestClient, repo: MemoryRepository) -> None:
    record = add_approved(repo, 1)
    repo.retire(record.id, vault_revision=REVISION, actor="tester", reason="no longer true")
    body = client.get(f"/api/memory/notes/{record.id}").json()
    assert body["status"] == "retired"
    assert [event["action"] for event in body["lifecycle"]] == ["retire"]
    assert "no longer true" not in client.get(f"/api/memory/notes/{record.id}").text


def test_candidate_detail_covers_pending_and_conflict_only(
    client: TestClient, repo: MemoryRepository
) -> None:
    pending = add_candidate(repo, 1)
    flagged = add_candidate(repo, 2)
    repo.transition(flagged.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    rejected = add_candidate(repo, 3)
    repo.transition(rejected.id, expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED)
    approved = add_approved(repo, 4)
    assert client.get(f"/api/memory/candidates/{pending.id}").json()["status"] == "pending"
    body = client.get(f"/api/memory/candidates/{flagged.id}").json()
    assert body["status"] == "conflict" and [r["action"] for r in body["reviews"]] == [
        "flag_conflict"
    ]
    for record in (rejected, approved):
        assert client.get(f"/api/memory/candidates/{record.id}").status_code == 404
    for record in (pending, flagged, rejected):
        assert client.get(f"/api/memory/notes/{record.id}").status_code == 404


@pytest.mark.parametrize(
    "memory_id",
    [
        "00000000-0000-4000-8000-000000000000",
        "not-an-id",
        "x" * 300,
        "<b>",
        "%00",
        "1",
        "%20",
    ],
)
def test_unknown_or_malformed_ids_are_a_plain_not_found(client: TestClient, memory_id: str) -> None:
    for kind in ("notes", "candidates"):
        response = client.get(f"/api/memory/{kind}/{memory_id}")
        assert response.status_code == 404
        assert response.json() == {"detail": "not_found"}
        assert memory_id not in response.text or memory_id == "1"


def test_only_the_canonical_lowercase_uuid_is_accepted(
    client: TestClient, repo: MemoryRepository
) -> None:
    record = add_approved(repo, 1)
    canonical = str(record.id)
    assert client.get(f"/api/memory/notes/{canonical}").status_code == 200
    spellings = [
        canonical.upper(),
        canonical.replace("-", ""),
        "{" + canonical + "}",
        "urn:uuid:" + canonical,
        canonical + "%0a",
        " " + canonical,
        canonical + " ",
        canonical[:-1] + canonical[-1].upper() if canonical[-1].isalpha() else canonical.upper(),
        canonical.replace("-", "\u2010", 1),
    ]
    for spelling in spellings:
        response = client.get(f"/api/memory/notes/{spelling}")
        assert response.status_code == 404, spelling
        assert response.json() == {"detail": "not_found"}


def test_detail_storage_errors_are_503_with_a_fixed_body(database: Database) -> None:
    class Broken(MemoryRepository):
        def get(self, memory_id):
            raise MemoryRepositoryError("leaky detail path")

    response = build_client(Broken(database)).get(f"/api/memory/notes/{uuid4()}")
    assert response.status_code == 503
    assert response.json() == {"detail": "storage_unavailable"}
    assert "leaky" not in response.text


def test_detail_text_is_only_ever_a_json_string(
    client: TestClient, repo: MemoryRepository
) -> None:
    record = add_approved(
        repo, 1, content=INJECTION, source=INJECTION, project=INJECTION, tags=(INJECTION,)
    )
    response = client.get(f"/api/memory/notes/{record.id}")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["content"] == INJECTION


def test_detail_and_search_change_nothing(client: TestClient, repo: MemoryRepository) -> None:
    record = add_approved(repo, 1)
    before = (repo.get(record.id), repo.review_events(record.id), repo.lifecycle_events(record.id))
    client.get(f"/api/memory/notes/{record.id}")
    client.get("/api/memory/notes", params={"q": "memory"})
    assert (
        repo.get(record.id), repo.review_events(record.id), repo.lifecycle_events(record.id)
    ) == before
