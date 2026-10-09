"""Human approval store, executor wrapper and API, using fake tools and fake clocks only."""

import asyncio
import re
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.approvals import create_approvals_router
from backend.auth.limiter import LoginLimiter
from backend.auth.middleware import is_public
from backend.auth.passwords import hash_passphrase
from backend.auth.service import AuthService
from backend.core.config import Settings
from backend.core.database import Database
from backend.tools.approvals import (
    REDACTED,
    ApprovalQueueFull,
    ApprovalState,
    ApprovalStore,
    ApprovalStoreError,
    DecisionOutcome,
    summarize_arguments,
)
from backend.tools.confirmed import ConfirmedExecutor
from backend.tools.contract import (
    PermissionLevel,
    ToolCall,
    ToolContext,
    ToolErrorCode,
    ToolSpec,
    ToolStatus,
)
from backend.tools.permission import PermissionPolicy
from backend.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
HEADERS = {"X-Jarvis-Confirm": "1"}
LEAK = "sk" + "-" + "LIVE0123456789abcdefLEAK"  # assembled so scanners do not flag it


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeTool:
    def __init__(self, name: str = "fake.write", level: PermissionLevel = PermissionLevel.RED):
        self.spec = ToolSpec(
            name=name,
            description="Fake confirm-required tool.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 500}},
                "required": ["text"],
            },
            output_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            permission=level,
            environment="local",
            timeout_seconds=1.0,
        )
        self.runs: list[tuple[str, bool]] = []

    async def run(self, arguments, context: ToolContext):
        self.runs.append((arguments["text"], context.confirmed))
        return {"text": arguments["text"]}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "approvals.sqlite3")
    db.initialize()
    return db


@pytest.fixture
def store(database: Database, clock: Clock) -> ApprovalStore:
    return ApprovalStore(database, ttl_seconds=60, clock=clock)


def make_call(text: str = "hello", call_id: str = "c1", tool: str = "fake.write") -> ToolCall:
    return ToolCall(call_id, tool, {"text": text})


def build(store: ApprovalStore, clock: Clock, *, policy=None, tool=None, sleep=None):
    tool = tool or FakeTool()
    registry = ToolRegistry(policy or PermissionPolicy(), cancel_grace_seconds=0.1)
    registry.register(tool)
    kwargs = {"clock": clock}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return ConfirmedExecutor(registry, store.requester(), **kwargs), tool


def run(coro):
    return asyncio.run(coro)


# ----- summary and redaction -----


def test_summary_is_fixed_shape_truncated_and_redacted() -> None:
    summary = summarize_arguments(
        {
            "path": "notes/" + "x y " * 200,
            "api_key": "plain-looking",
            "note": LEAK,
            "bearer": "Bearer abc.def.ghi",
            "blob": "A" * 40,
            "nested": {"password": "hunter2", "a": 1},
            "items": [1, 2, 3],
            "z1": True,
            "z2": 3,
            "ctrl": "a\x1b[31mb\nc‮d",
        }
    )
    assert set(summary) == {"fields", "more_fields", "argument_bytes", "digest_prefix"}
    assert len(summary["fields"]) == 8 and summary["more_fields"] == 2
    previews = {f["name"]: f["preview"] for f in summary["fields"]}
    assert previews["api_key"] == REDACTED
    assert previews["bearer"] == REDACTED
    assert previews["blob"] == REDACTED and previews["note"] == REDACTED
    assert previews["nested"] == "object(2 keys)" and previews["items"] == "array(3 items)"
    assert len(previews["path"]) <= 80 and previews["path"].endswith("…")
    text = repr(summary)
    assert LEAK not in text and "hunter2" not in text and "plain-looking" not in text
    assert "\x1b" not in text and "‮" not in text and "\n" not in text
    assert len(summary["digest_prefix"]) == 12


def test_stored_row_never_contains_secret_values(store: ApprovalStore, database: Database) -> None:
    store.request(ToolCall("c1", "fake.write", {"text": "x", "token": LEAK}))
    with sqlite3.connect(database.path) as connection:
        dump = "\n".join(str(row) for row in connection.execute("SELECT * FROM tool_approvals"))
    assert LEAK not in dump


# ----- store -----


def test_request_is_pending_with_expiry_and_is_deduplicated(store: ApprovalStore) -> None:
    first = store.request(make_call())
    again = store.request(make_call(call_id="other"))
    assert first.id == again.id and first.state is ApprovalState.PENDING
    assert first.expires_at - first.requested_at == timedelta(seconds=60)
    assert store.request(make_call("different")).id != first.id
    assert len(store.list_pending()) == 2


def test_pending_queue_is_bounded(database: Database, clock: Clock) -> None:
    small = ApprovalStore(database, max_pending=2, clock=clock)
    small.request(make_call("a"))
    small.request(make_call("b"))
    with pytest.raises(ApprovalQueueFull):
        small.request(make_call("c"))
    clock.advance(10_000)  # expired requests free their slots
    small.request(make_call("c"))


@pytest.mark.parametrize("ttl", [0, 5, 3601, True, "60", None])
def test_ttl_must_be_sane(database: Database, ttl: object) -> None:
    with pytest.raises(ValueError):
        ApprovalStore(database, ttl_seconds=ttl)  # type: ignore[arg-type]


def test_decision_is_applied_exactly_once(store: ApprovalStore) -> None:
    request = store.request(make_call())
    assert store.decide(request.id, True) is DecisionOutcome.APPLIED
    assert store.decide(request.id, True) is DecisionOutcome.NOT_PENDING
    assert store.decide(request.id, False) is DecisionOutcome.NOT_PENDING
    assert store.state_of(request.id) is ApprovalState.APPROVED
    denied = store.request(make_call("other"))
    assert store.decide(denied.id, False) is DecisionOutcome.APPLIED
    assert store.decide(denied.id, True) is DecisionOutcome.NOT_PENDING
    assert store.state_of(denied.id) is ApprovalState.DENIED
    assert store.list_pending() == []


def test_unknown_and_malformed_ids_are_not_found(store: ApprovalStore) -> None:
    assert store.decide(str(uuid4()), True) is DecisionOutcome.NOT_FOUND
    assert store.decide("not-a-uuid", True) is DecisionOutcome.NOT_FOUND
    assert store.decide("' OR 1=1 --", True) is DecisionOutcome.NOT_FOUND
    assert store.state_of(str(uuid4())) is None


def test_expired_request_cannot_be_decided_or_listed(store: ApprovalStore, clock: Clock) -> None:
    request = store.request(make_call())
    clock.advance(60)
    assert store.list_pending() == []
    assert store.decide(request.id, True) is DecisionOutcome.EXPIRED
    assert store.state_of(request.id) is ApprovalState.EXPIRED
    assert not store.consume("fake.write", make_call().arguments)


def test_approved_but_expired_approval_cannot_be_consumed(
    store: ApprovalStore, clock: Clock
) -> None:
    request = store.request(make_call())
    store.decide(request.id, True)
    clock.advance(61)
    assert store.state_of(request.id) is ApprovalState.EXPIRED
    assert store.consume("fake.write", make_call().arguments) is False


def test_consume_is_one_shot_and_bound_to_tool_and_exact_arguments(store: ApprovalStore) -> None:
    request = store.request(make_call("approved text"))
    store.decide(request.id, True)
    assert store.consume("fake.write", {"text": "approved text "}) is False  # swapped arguments
    assert store.consume("fake.write", {"text": "approved text", "x": 1}) is False
    assert store.consume("other.tool", {"text": "approved text"}) is False
    assert store.consume("fake.write", {"text": "approved text"}) is True
    assert store.consume("fake.write", {"text": "approved text"}) is False  # replay


def test_consume_ignores_key_order_and_rejects_unusable_input(store: ApprovalStore) -> None:
    call = ToolCall("c1", "fake.write", {"b": 1, "a": 2})
    store.decide(store.request(call).id, True)
    assert store.consume("fake.write", {"a": 2, "b": 1}) is True
    assert store.consume("Not A Name", {"a": 2}) is False
    assert store.consume("fake.write", {"a": float("nan")}) is False


def test_denied_and_pending_requests_cannot_be_consumed(store: ApprovalStore) -> None:
    pending = store.request(make_call("p"))
    assert store.consume("fake.write", {"text": "p"}) is False
    store.decide(pending.id, False)
    assert store.consume("fake.write", {"text": "p"}) is False


def test_concurrent_consume_succeeds_exactly_once(store: ApprovalStore) -> None:
    store.decide(store.request(make_call()).id, True)
    results: list[bool] = []
    barrier = threading.Barrier(16)

    def attempt() -> None:
        barrier.wait()
        results.append(store.consume("fake.write", {"text": "hello"}))

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1 and len(results) == 16


def test_database_refuses_to_redecide_rebind_or_unconsume(
    store: ApprovalStore, database: Database
) -> None:
    approved = store.request(make_call())
    store.decide(approved.id, True)
    store.consume("fake.write", {"text": "hello"})
    denied = store.request(make_call("d"))
    store.decide(denied.id, False)
    statements = [
        ("UPDATE tool_approvals SET state = 'denied' WHERE id = ?", approved.id),
        ("UPDATE tool_approvals SET state = 'approved' WHERE id = ?", denied.id),
        ("UPDATE tool_approvals SET consumed_at = NULL WHERE id = ?", approved.id),
        ("UPDATE tool_approvals SET args_digest = ? WHERE id = ?", None),
        ("UPDATE tool_approvals SET expires_at = '2099-01-01T00:00:00.000000Z' WHERE id = ?",
         denied.id),
    ]
    with sqlite3.connect(database.path) as connection:
        for sql, arg in statements:
            params = ("0" * 64, approved.id) if arg is None else (arg,)
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(sql, params)
        with pytest.raises(sqlite3.IntegrityError):  # consumed requires approved
            connection.execute(
                "UPDATE tool_approvals SET consumed_at = 'x' WHERE id = ?", (denied.id,)
            )


def test_storage_failure_is_an_error_not_a_grant(tmp_path: Path, clock: Clock) -> None:
    broken = ApprovalStore(Database(tmp_path / "missing" / "nope.sqlite3"), clock=clock)
    with pytest.raises(ApprovalStoreError):
        broken.list_pending()
    assert broken.consume("fake.write", {"text": "hello"}) is False


# ----- executor -----


def test_confirm_required_tool_is_refused_and_queued_without_running(
    store: ApprovalStore, clock: Clock
) -> None:
    executor, tool = build(store, clock)
    outcome = run(executor.execute(make_call()))
    assert outcome.result.status is ToolStatus.DENIED
    assert outcome.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert outcome.approval_state is ApprovalState.PENDING and outcome.approval_id
    assert tool.runs == []
    assert [a.id for a in store.list_pending()] == [outcome.approval_id]
    assert run(executor.execute(make_call())).approval_id == outcome.approval_id


def test_approved_call_runs_once_then_needs_a_new_approval(
    store: ApprovalStore, clock: Clock
) -> None:
    executor, tool = build(store, clock)
    first = run(executor.execute(make_call()))
    store.decide(first.approval_id, True)
    ran = run(executor.execute(make_call(call_id="c2")))
    assert ran.result.status is ToolStatus.OK and tool.runs == [("hello", True)]
    replay = run(executor.execute(make_call(call_id="c3")))
    assert replay.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert replay.approval_id != first.approval_id and tool.runs == [("hello", True)]


def test_approval_does_not_cover_swapped_arguments_or_tools(
    store: ApprovalStore, clock: Clock
) -> None:
    executor, tool = build(store, clock)
    first = run(executor.execute(make_call("benign")))
    store.decide(first.approval_id, True)
    swapped = run(executor.execute(make_call("malicious")))
    assert swapped.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert swapped.approval_id != first.approval_id and tool.runs == []
    assert run(executor.execute(make_call("benign"))).result.status is ToolStatus.OK
    assert tool.runs == [("benign", True)]


def test_expired_approval_does_not_run(store: ApprovalStore, clock: Clock) -> None:
    executor, tool = build(store, clock)
    first = run(executor.execute(make_call()))
    store.decide(first.approval_id, True)
    clock.advance(61)
    late = run(executor.execute(make_call()))
    assert late.result.error is ToolErrorCode.CONFIRMATION_REQUIRED and tool.runs == []


def test_denied_request_is_a_permission_denial_when_waiting(
    store: ApprovalStore, clock: Clock
) -> None:
    async def human_denies(_seconds: float) -> None:
        store.decide(store.list_pending()[0].id, False)

    executor, tool = build(store, clock, sleep=human_denies)
    outcome = run(executor.execute(make_call(), wait_seconds=30))
    assert outcome.result.error is ToolErrorCode.PERMISSION_DENIED
    assert outcome.approval_state is ApprovalState.DENIED and tool.runs == []


def test_waiting_call_runs_when_the_human_approves(store: ApprovalStore, clock: Clock) -> None:
    async def human_approves(_seconds: float) -> None:
        store.decide(store.list_pending()[0].id, True)

    executor, tool = build(store, clock, sleep=human_approves)
    outcome = run(executor.execute(make_call(), wait_seconds=30))
    assert outcome.result.status is ToolStatus.OK and tool.runs == [("hello", True)]


def test_waiting_without_a_decision_times_out_as_not_approved(
    store: ApprovalStore, clock: Clock
) -> None:
    async def time_passes(seconds: float) -> None:
        clock.advance(seconds + 5)

    executor, tool = build(store, clock, sleep=time_passes)
    outcome = run(executor.execute(make_call(), wait_seconds=20))
    assert outcome.result.error is ToolErrorCode.CONFIRMATION_REQUIRED
    assert tool.runs == [] and outcome.approval_state is ApprovalState.PENDING


def test_request_expiring_while_waiting_is_denied(store: ApprovalStore, clock: Clock) -> None:
    async def time_passes(_seconds: float) -> None:
        clock.advance(61)

    executor, tool = build(store, clock, sleep=time_passes)
    outcome = run(executor.execute(make_call(), wait_seconds=600))
    assert outcome.result.error is ToolErrorCode.PERMISSION_DENIED
    assert outcome.approval_state is ApprovalState.EXPIRED and tool.runs == []


def test_concurrent_executions_of_one_approval_run_the_tool_once(
    store: ApprovalStore, clock: Clock
) -> None:
    executor, tool = build(store, clock)
    first = run(executor.execute(make_call()))
    store.decide(first.approval_id, True)

    async def race():
        return await asyncio.gather(
            *(executor.execute(make_call(call_id=f"r{i}")) for i in range(8))
        )

    outcomes = run(race())
    assert [o.result.status for o in outcomes].count(ToolStatus.OK) == 1
    assert tool.runs == [("hello", True)]


def test_policy_deny_beats_approval_and_creates_no_request(
    store: ApprovalStore, clock: Clock
) -> None:
    executor, tool = build(store, clock, policy=PermissionPolicy(deny={"fake.write"}))
    outcome = run(executor.execute(make_call()))
    assert outcome.result.error is ToolErrorCode.PERMISSION_DENIED
    assert outcome.approval_id is None and store.list_pending() == [] and tool.runs == []


def test_invalid_arguments_and_unknown_tools_queue_nothing(
    store: ApprovalStore, clock: Clock
) -> None:
    executor, tool = build(store, clock)
    bad = run(executor.execute(ToolCall("c1", "fake.write", {"text": 5})))
    assert bad.result.status is ToolStatus.INVALID_ARGUMENTS
    unknown = run(executor.execute(make_call(tool="nope.tool")))
    assert unknown.result.error is ToolErrorCode.UNKNOWN_TOOL
    assert store.list_pending() == [] and tool.runs == []


def test_green_tool_runs_without_asking(store: ApprovalStore, clock: Clock) -> None:
    executor, tool = build(store, clock, tool=FakeTool("fake.read", PermissionLevel.GREEN))
    outcome = run(executor.execute(make_call(tool="fake.read")))
    assert outcome.result.status is ToolStatus.OK and store.list_pending() == []
    assert tool.runs == [("hello", False)]


def test_storage_failure_means_not_approved(store: ApprovalStore, clock: Clock) -> None:
    executor, tool = build(store, clock)

    def broken(*_args, **_kwargs):
        raise ApprovalStoreError("down")

    store.request = broken  # type: ignore[method-assign]
    outcome = run(executor.execute(make_call()))
    assert outcome.result.error is ToolErrorCode.PERMISSION_DENIED and tool.runs == []


def test_full_queue_means_not_approved(database: Database, clock: Clock) -> None:
    full = ApprovalStore(database, max_pending=1, clock=clock)
    executor, tool = build(full, clock)
    run(executor.execute(make_call("a")))
    outcome = run(executor.execute(make_call("b")))
    assert outcome.result.error is ToolErrorCode.PERMISSION_DENIED and tool.runs == []


# ----- the agent side cannot approve -----


def test_executor_and_requester_expose_no_way_to_decide(store: ApprovalStore, clock: Clock) -> None:
    executor, _tool = build(store, clock)
    requester = store.requester()
    for obj in (executor, requester):
        assert not any("decide" in name or "approve" in name for name in dir(obj))
    assert not hasattr(ToolContext, "approvals")
    assert set(ToolContext.__dataclass_fields__) == {
        "call_id", "tool_name", "permission", "confirmed", "timeout_seconds", "cancellation",
    }


def test_only_the_api_module_calls_decide() -> None:
    callers = []
    for path in (ROOT / "backend").rglob("*.py"):
        text = path.read_text()
        if re.search(r"\bApprovalStore\b", text):
            callers.append(path.relative_to(ROOT).as_posix())
    assert sorted(callers) == [
        "backend/api/app.py",
        "backend/api/approvals.py",
        "backend/tools/approvals.py",
    ]
    for path in (ROOT / "backend" / "tools").glob("*.py"):
        if path.name != "approvals.py":
            assert ".decide(" not in path.read_text(), path.name


def test_nothing_is_registered_by_default() -> None:
    for path in (ROOT / "backend").rglob("*.py"):
        if "tools" in path.parts:
            continue
        text = path.read_text()
        assert "ToolRegistry" not in text and "ConfirmedExecutor" not in text, path.name


# ----- migration -----


def test_v8_database_upgrades_to_approvals_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "v8.sqlite3"
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tasks (id, goal, status, created_at, updated_at) "
            "VALUES ('t1', 'goal', 'pending', 't', 't')"
        )
        connection.execute("DROP TABLE tool_approvals")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("PRAGMA user_version = 8")
    assert not database.is_ready()
    database.initialize()
    assert database.is_ready()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM tasks").fetchall() == [("t1",)]
        assert connection.execute("SELECT COUNT(*) FROM tool_approvals").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# ----- API -----


def api_app(store: ApprovalStore) -> FastAPI:
    app = FastAPI()
    app.include_router(create_approvals_router(store))
    return app


@pytest.fixture
def client(store: ApprovalStore) -> Iterator[TestClient]:
    with TestClient(api_app(store), base_url="http://testserver") as test_client:
        yield test_client


def test_list_is_empty_then_shows_redacted_pending_requests(
    client: TestClient, store: ApprovalStore
) -> None:
    assert client.get("/api/approvals").json() == {"approvals": []}
    store.request(ToolCall("c1", "fake.write", {"text": "<script>x</script>", "token": LEAK}))
    response = client.get("/api/approvals")
    assert response.headers["cache-control"] == "no-store"
    [item] = response.json()["approvals"]
    assert set(item) == {"id", "tool_name", "summary", "requested_at", "expires_at", "state"}
    assert item["state"] == "pending" and item["tool_name"] == "fake.write"
    assert LEAK not in response.text
    previews = {f["name"]: f["preview"] for f in item["summary"]["fields"]}
    assert previews["token"] == REDACTED and previews["text"] == "<script>x</script>"


def test_approve_and_deny_record_the_decision_only(
    client: TestClient, store: ApprovalStore
) -> None:
    executor, tool = build(store, Clock())
    ok = store.request(make_call("a"))
    no = store.request(make_call("b"))
    response = client.post(f"/api/approvals/{ok.id}/approve", headers=HEADERS)
    assert response.status_code == 200 and response.json() == {"id": ok.id, "state": "approved"}
    response = client.post(f"/api/approvals/{no.id}/deny", headers=HEADERS)
    assert response.json() == {"id": no.id, "state": "denied"}
    assert store.state_of(ok.id) is ApprovalState.APPROVED and tool.runs == []
    assert client.get("/api/approvals").json() == {"approvals": []}


def test_decision_errors_use_fixed_codes(
    client: TestClient, store: ApprovalStore, clock: Clock
) -> None:
    request = store.request(make_call())
    client.post(f"/api/approvals/{request.id}/deny", headers=HEADERS)
    again = client.post(f"/api/approvals/{request.id}/approve", headers=HEADERS)
    assert (again.status_code, again.json()["detail"]) == (409, "approval_not_pending")
    missing = client.post(f"/api/approvals/{uuid4()}/approve", headers=HEADERS)
    assert (missing.status_code, missing.json()["detail"]) == (404, "approval_not_found")
    garbage = client.post("/api/approvals/%27%20OR%201=1/approve", headers=HEADERS)
    assert garbage.status_code == 404
    late = store.request(make_call("late"))
    clock.advance(61)
    expired = client.post(f"/api/approvals/{late.id}/approve", headers=HEADERS)
    assert (expired.status_code, expired.json()["detail"]) == (410, "approval_expired")


def test_unsafe_requests_need_the_header_and_a_same_origin(
    client: TestClient, store: ApprovalStore
) -> None:
    request = store.request(make_call())
    url = f"/api/approvals/{request.id}/approve"
    assert client.post(url).json()["detail"] == "confirm_header_required"
    cross = {**HEADERS, "Origin": "https://evil.example"}
    assert client.post(url, headers=cross).json()["detail"] == "forbidden"
    referer = {**HEADERS, "Referer": "https://evil.example/page"}
    assert client.post(url, headers=referer).status_code == 403
    site = {**HEADERS, "Sec-Fetch-Site": "cross-site"}
    assert client.post(url, headers=site).status_code == 403
    assert client.post(url, headers={"X-Jarvis-Confirm": "yes"}).status_code == 403
    assert store.state_of(request.id) is ApprovalState.PENDING
    same = {**HEADERS, "Origin": "http://testserver"}
    assert client.post(url, headers=same).status_code == 200


def test_get_cannot_change_state_and_other_methods_are_rejected(
    client: TestClient, store: ApprovalStore
) -> None:
    request = store.request(make_call())
    assert client.get(f"/api/approvals/{request.id}/approve").status_code == 405
    assert client.put(f"/api/approvals/{request.id}/approve", headers=HEADERS).status_code == 405
    assert store.state_of(request.id) is ApprovalState.PENDING


def test_storage_failure_is_503_without_details(client: TestClient, store: ApprovalStore) -> None:
    def broken(*_args, **_kwargs):
        raise ApprovalStoreError("sqlite said /private/path")

    store.list_pending = broken  # type: ignore[method-assign]
    store.decide = broken  # type: ignore[method-assign]
    for response in (
        client.get("/api/approvals"),
        client.post(f"/api/approvals/{uuid4()}/approve", headers=HEADERS),
    ):
        assert response.status_code == 503 and "private" not in response.text


# ----- app wiring and login layer -----

PASSPHRASE = "fake passphrase for tests only"
HOST = "https://testserver"


@pytest.fixture
def auth_client(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    settings = Settings(
        db_path=tmp_path / "auth.sqlite3",
        auth_passphrase_hash=hash_passphrase(PASSPHRASE, n=16, r=1, p=1),
        auth_signing_key="k" * 40,
        auth_cookie_secure=True,
    )
    auth = AuthService.from_settings(settings, limiter=LoginLimiter(failure_delay=0))
    with TestClient(create_app(settings, None, auth=auth), base_url=HOST) as test_client:
        yield test_client, auth.cookie_name


def test_approvals_are_not_public() -> None:
    for path in ("/api/approvals", "/approvals", "/static/approvals.js", "/static/approvals.css"):
        assert not is_public("GET", path)
    assert not is_public("POST", f"/api/approvals/{uuid4()}/approve")


def test_login_layer_protects_the_page_and_every_endpoint(
    auth_client: tuple[TestClient, str],
) -> None:
    client, cookie_name = auth_client
    request_id = str(uuid4())
    page = client.get("/approvals", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.get("/api/approvals").status_code == 401
    for action in ("approve", "deny"):
        url = f"/api/approvals/{request_id}/{action}"
        signed_in_headers = {**HEADERS, "Origin": HOST}
        assert client.post(url, headers=signed_in_headers).status_code == 401
    login = client.post(
        "/api/auth/login", json={"passphrase": PASSPHRASE}, headers={"Origin": HOST}
    )
    assert login.status_code == 200 and client.cookies.get(cookie_name)
    assert client.get("/api/approvals").json() == {"approvals": []}
    assert client.get("/approvals").status_code == 200
    url = f"/api/approvals/{request_id}/approve"
    evil = {**HEADERS, "Origin": "https://evil.example"}
    assert client.post(url, headers=evil).status_code == 403
    assert client.post(url, headers=HEADERS).status_code == 403  # login layer wants Origin
    assert client.post(url, headers={**HEADERS, "Origin": HOST}).status_code == 404


def test_app_serves_the_page_and_registers_no_tools_by_default(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "plain.sqlite3"))
    with TestClient(app) as client:
        assert client.get("/approvals").status_code == 200
        assert client.get("/api/approvals").json() == {"approvals": []}
