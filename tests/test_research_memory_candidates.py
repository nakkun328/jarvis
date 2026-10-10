"""Research results become PENDING memory candidates only on a human request, then need review."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.research_memory import create_research_memory_router
from backend.auth.limiter import LoginLimiter
from backend.auth.passwords import hash_passphrase
from backend.auth.service import AuthService
from backend.core.config import Settings
from backend.core.database import Database
from backend.memory.model import MemoryOrigin
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.writer import MemoryWriter
from backend.research.memory_candidates import (
    MAX_CANDIDATES_PER_SESSION,
    ResearchMemoryCandidates,
)
from backend.research.models import (
    ConflictKind,
    FailureReason,
    ResearchLevel,
    ResearchStatus,
)
from backend.research.repository import ResearchRepository

HEADERS = {"X-Jarvis-Confirm": "1"}
RETRIEVED = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
URL = "https://docs.example.test/foo"
QUOTE = "The Foo cache keeps entries for 60 seconds."
FREE_TEXT = "free-text-result-marker-should-never-be-copied"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database(tmp_path / "r2m.sqlite3")
        self.database.initialize()
        self.research = ResearchRepository(self.database)
        self.memory = MemoryRepository(self.database)
        self.vault_root = tmp_path / "vault"
        app = FastAPI()
        app.include_router(
            create_research_memory_router(ResearchMemoryCandidates(self.research, self.memory))
        )
        self.client = TestClient(app)

    def session(self, claims: int = 1, *, complete: bool = True, conflict: bool = False):
        session = self.research.create_session("How long is the cache?", ResearchLevel.STANDARD)
        self.research.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
        source = self.research.add_source(
            session.id,
            url=URL,
            final_url=URL,
            retrieved_at=RETRIEVED,
            content_digest=hashlib.sha256(b"x").hexdigest(),
            title="Foo docs",
        )
        for index in range(claims):
            self.research.add_claim(
                session.id,
                claim_text=f"Foo cache TTL is 60 seconds ({index}).",
                source_id=source.id,
                quote=QUOTE,
            )
        if conflict:
            other = self.research.add_source(
                session.id,
                url="https://other.example.test/x",
                final_url="https://other.example.test/x",
                retrieved_at=RETRIEVED,
                content_digest=hashlib.sha256(b"y").hexdigest(),
            )
            self.research.add_conflict(
                session.id,
                ConflictKind.NUMBER_MISMATCH,
                self.research.list_claims(session.id)[0].id,
                other_source_id=other.id,
            )
        if complete:
            self.research.set_result(session.id, FREE_TEXT)
        return session

    def url(self, session_id) -> str:
        return f"/api/research/sessions/{session_id}/memory-candidates"

    def post(self, session_id, headers=HEADERS):
        return self.client.post(self.url(session_id), headers=headers)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_happy_path_creates_pending_research_candidates_with_provenance(env: Env) -> None:
    session = env.session(claims=2)
    response = env.post(session.id)
    assert response.status_code == 201
    body = response.json()
    assert body["created"] == 2 and body["omitted"] == 0 and body["eligible"] is True
    assert len(body["candidates"]) == 2
    for item in body["candidates"]:
        assert item["status"] == "pending"
        assert item["origin"] == "research"
        assert item["source"] == f"research:{session.id}"
        assert "research" in item["tags"]
        for expected in (QUOTE, URL, "2026-10-07", str(session.id)):
            assert expected in item["content"]
        assert FREE_TEXT not in item["content"]
    stored = env.memory.list_by_status(MemoryStatus.PENDING)
    assert len(stored) == 2
    assert all(s.record.origin is MemoryOrigin.RESEARCH for s in stored)
    assert env.memory.list_by_status(MemoryStatus.APPROVED) == []


def test_second_request_is_idempotent(env: Env) -> None:
    session = env.session(claims=2)
    first = env.post(session.id).json()
    second = env.post(session.id)
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert [c["id"] for c in second.json()["candidates"]] == [c["id"] for c in first["candidates"]]
    assert len(env.memory.list_by_status(MemoryStatus.PENDING)) == 2
    listed = env.client.get(env.url(session.id)).json()
    assert len(listed["candidates"]) == 2 and listed["created"] == 0


def test_rejected_candidate_is_not_recreated(env: Env) -> None:
    session = env.session()
    first = env.post(session.id).json()["candidates"][0]
    env.memory.transition(
        UUID(first["id"]), expected=MemoryStatus.PENDING, new=MemoryStatus.REJECTED, actor="t"
    )
    again = env.post(session.id).json()
    assert again["created"] == 0 and again["candidates"][0]["status"] == "rejected"


def test_cap_per_session(env: Env) -> None:
    session = env.session(claims=MAX_CANDIDATES_PER_SESSION + 2)
    body = env.post(session.id).json()
    assert body["created"] == MAX_CANDIDATES_PER_SESSION and body["omitted"] == 2


def test_refusals_use_fixed_codes(env: Env) -> None:
    running = env.session(complete=False)
    assert env.post(running.id).json() == {"detail": "session_not_completed"}
    assert env.post(running.id).status_code == 409
    empty = env.session(claims=0)
    response = env.post(empty.id)
    assert (response.status_code, response.json()) == (409, {"detail": "no_verified_claims"})
    missing = env.post(uuid4())
    assert (missing.status_code, missing.json()) == (404, {"detail": "session_not_found"})
    assert env.post("not-a-uuid").status_code == 404
    assert env.memory.list_by_status(MemoryStatus.PENDING) == []


def test_failed_session_is_refused(env: Env) -> None:
    session = env.session(complete=False)
    env.research.transition(
        session.id,
        ResearchStatus.RUNNING,
        ResearchStatus.FAILED,
        failure_reason=FailureReason.NO_RESULTS,
    )
    assert env.post(session.id).json() == {"detail": "session_not_completed"}
    listed = env.client.get(env.url(session.id)).json()
    assert listed["eligible"] is False and listed["candidates"] == []


def test_confirm_header_and_same_origin_are_required(env: Env) -> None:
    session = env.session()
    no_header = env.post(session.id, headers={})
    assert (no_header.status_code, no_header.json()) == (403, {"detail": "confirm_header_required"})
    cross = env.post(session.id, headers={**HEADERS, "Origin": "http://evil.example"})
    assert (cross.status_code, cross.json()) == (403, {"detail": "forbidden"})
    assert env.memory.list_by_status(MemoryStatus.PENDING) == []


def test_login_layer_protects_the_endpoint(tmp_path: Path) -> None:
    passphrase = "fake passphrase for tests only"
    host = "https://testserver"
    settings = Settings(
        db_path=tmp_path / "auth.sqlite3",
        auth_passphrase_hash=hash_passphrase(passphrase, n=16, r=1, p=1),
        auth_signing_key="k" * 40,
        auth_cookie_secure=True,
    )
    auth = AuthService.from_settings(settings, limiter=LoginLimiter(failure_delay=0))
    with TestClient(create_app(settings, None, auth=auth), base_url=host) as client:
        url = f"/api/research/sessions/{uuid4()}/memory-candidates"
        assert client.get(url).status_code == 401
        assert client.post(url, headers={**HEADERS, "Origin": host}).status_code == 401
        login = client.post(
            "/api/auth/login", json={"passphrase": passphrase}, headers={"Origin": host}
        )
        assert login.status_code == 200
        assert client.post(url, headers={**HEADERS, "Origin": host}).status_code == 404


def test_approval_needs_review_and_only_then_writes_a_note_with_provenance(
    env: Env,
) -> None:
    session = env.session()
    candidate_id = UUID(env.post(session.id).json()["candidates"][0]["id"])
    assert not env.vault_root.exists()  # staging never touches a vault
    assert env.memory.get(candidate_id).status is MemoryStatus.PENDING

    writer = MemoryWriter(env.memory, ObsidianVault(env.vault_root))
    approved = writer.approve(candidate_id, actor="reviewer:test")
    assert approved.status is MemoryStatus.APPROVED
    note = ObsidianVault(env.vault_root).read(candidate_id)
    assert note is not None
    assert note.metadata["origin"] == "research"
    assert note.metadata["source"] == f"research:{session.id}"
    for expected in (QUOTE, URL, "2026-10-07"):
        assert expected in note.body
    assert [p.name for p in env.vault_root.iterdir()] == [f"{candidate_id}.md"]


def test_no_vault_is_needed_or_created_by_the_endpoint(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.post(env.session().id)
    assert not env.vault_root.exists()


def test_open_conflict_claims_are_not_offered(env: Env) -> None:
    session = env.session(claims=1, conflict=True)
    response = env.post(session.id)
    assert (response.status_code, response.json()) == (409, {"detail": "no_verified_claims"})


def test_only_the_api_handler_reaches_the_staging_code() -> None:
    root = Path(__file__).resolve().parents[1] / "backend"
    allowed = {
        root / "research" / "memory_candidates.py",
        root / "api" / "research_memory.py",
        root / "api" / "app.py",
    }
    pattern = re.compile(r"memory_candidates|ResearchMemoryCandidates|research_memory")
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path not in allowed and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    handler = (root / "api" / "research_memory.py").read_text(encoding="utf-8")
    assert "memory.writer" not in handler and "obsidian" not in handler
    assert "transition(" not in handler
    service = (root / "research" / "memory_candidates.py").read_text(encoding="utf-8")
    assert "memory.writer" not in service and "obsidian" not in service
    assert "result_text" not in service  # the free-text result is never read
