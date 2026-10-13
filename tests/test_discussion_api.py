"""The group discussion as a background task and its API (docs/discussion.md).

Fake providers and a temporary database only: no network, no keys.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.auth.limiter import LoginLimiter
from backend.auth.passwords import hash_passphrase
from backend.auth.service import AuthService
from backend.chat.persistence import SQLiteConversationStore
from backend.core.config import Settings
from backend.core.database import SCHEMA_VERSION, Database
from backend.discussion import DiscussionError
from backend.discussion.format import (
    format_message,
    format_notice,
    notice_code,
    parse_message,
)
from backend.discussion.service import DiscussionService
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError
from backend.providers.choices import ModelRegistry
from backend.tasks.models import (
    CHAT_TOOL_GOAL_PREFIX,
    DISCUSSION_GOAL_PREFIX,
    StepStatus,
    TaskStatus,
    chat_tool_goal,
    discussion_goal,
)
from backend.tasks.queue import TaskQueue
from backend.tasks.repository import TaskRepository

pytestmark = pytest.mark.usefixtures("no_network")

MODELS = ("openai:model-a", "gemini:model-b", "groq:model-c")
TOPIC = "TOPIC-SENTINEL should the club buy a new projector?"
PRESETS = ["設計者", "批判役", "現実担当"]
HEADERS = {"X-Jarvis-Confirm": "1"}
FINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class Fake:
    """Answers `<label>-reply-<n>`; can fail or wait for a gate before answering."""

    def __init__(self, label: str, *, fail: bool = False):
        self.name, self.model = "fake", label
        self.label, self.fail = label, fail
        self.block_at: int | None = None  # the call with this number never answers
        self.calls = 0
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        self.requests.append(request)
        if self.block_at == self.calls:
            await asyncio.Event().wait()
        if self.fail:
            raise ProviderError("PROVIDER-SECRET-DETAIL")
        return CompletionResponse(f"{self.label}-reply-{self.calls}", self.name, self.model)


class Env:
    def __init__(self, tmp_path: Path, **kwargs) -> None:
        self.db = Database(tmp_path / "db.sqlite3")
        self.db.initialize()
        self.providers = {m: Fake(m) for m in MODELS}
        self.default = Fake("moderator")
        self.ready = kwargs.pop("ready", lambda provider: True)
        self.registry = ModelRegistry(
            MODELS,
            self.default,
            env={},
            builder=lambda provider, model: self.providers[f"{provider}:{model}"],
            ready=lambda provider: self.ready(provider),
        )
        self.store = SQLiteConversationStore(self.db)
        self.service = DiscussionService(
            TaskQueue(TaskRepository(self.db)),
            self.store,
            self.registry,
            self.default,
            model_choices=MODELS,
            max_rounds=kwargs.pop("max_rounds", 3),
            per_day_limit=kwargs.pop("per_day_limit", 10),
            worker_poll_seconds=0.01,
            **kwargs,
        )

    @property
    def repo(self) -> TaskRepository:
        return self.service.queue.repository

    async def messages(self, conversation_id: UUID) -> list[tuple[str, str]]:
        page = await self.store.messages_page(conversation_id, limit=200)
        assert page is not None
        return [(m.role, m.content) for m in page.messages]

    async def run_until_final(self, limit: float = 10.0) -> None:
        stop = asyncio.Event()
        worker = asyncio.create_task(self.service.run_worker(stop))
        try:
            async with asyncio.timeout(limit):
                while True:
                    tasks = self.repo.list_tasks(limit=100)
                    if tasks and all(t.status in FINAL for t in tasks):
                        await asyncio.sleep(0.05)  # the worker's closing posts
                        return
                    await asyncio.sleep(0.01)
        finally:
            stop.set()
            await worker


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def run(coroutine):
    return asyncio.run(coroutine)


# ----- the message format -----


def test_format_round_trip_and_unprefixed_messages() -> None:
    parsed = parse_message(format_message("設計者", "本文 【と】 括弧"))
    assert (parsed.speaker, parsed.text) == ("設計者", "本文 【と】 括弧")
    assert parse_message("ただの文章").speaker is None
    assert parse_message("ただの文章").text == "ただの文章"
    assert parse_message("本文の途中に【設計者】").speaker is None
    notice = parse_message(format_notice("止めました。", "CALL_CAP"))
    assert notice.speaker == "お知らせ"
    assert notice_code(notice.text) == "CALL_CAP"
    assert notice_code("コードなし") is None


# ----- a full run -----


def test_full_run_posts_turns_in_order_with_prefixes_and_the_summary_last(env: Env) -> None:
    async def scenario() -> None:
        conversation_id, task_id = await env.service.start(TOPIC, PRESETS, 2)
        await env.run_until_final()
        task = env.repo.get_task(task_id)
        assert task.status is TaskStatus.COMPLETED and task.verified.value == "verified"
        messages = await env.messages(conversation_id)
        assert messages[0] == ("user", TOPIC)
        assert [role for role, _ in messages[1:]] == ["assistant"] * 7
        speakers = [parse_message(text).speaker for _, text in messages[1:]]
        assert speakers == [*PRESETS, *PRESETS, "まとめ"]
        assert parse_message(messages[1][1]).text.endswith("-reply-1")
        assert messages[-1][1].startswith("【まとめ】moderator-reply-")
        assert all(step.status is StepStatus.COMPLETED for step in task.steps)
        assert len(task.steps) == 7
        # The topic is in the conversation, and the conversation is listed with a derived title.
        listed = await env.store.list_conversations(limit=10)
        assert listed.items[0].title.startswith("TOPIC-SENTINEL")

    run(scenario())


def test_rounds_are_clamped_and_validated(tmp_path: Path) -> None:
    env = Env(tmp_path, max_rounds=1)

    async def scenario() -> None:
        _, task_id = await env.service.start(TOPIC, PRESETS, 3)
        assert len(env.repo.get_task(task_id).steps) == 3 * 1 + 1
        for bad in (0, -1, True, "2", 1.5):
            with pytest.raises(DiscussionError) as caught:
                await env.service.start(TOPIC, PRESETS, bad)
            assert str(caught.value) == "DISCUSSION_ROUNDS_INVALID"

    run(scenario())


def test_default_rounds_are_two(env: Env) -> None:
    async def scenario() -> None:
        _, task_id = await env.service.start(TOPIC, PRESETS)
        assert len(env.repo.get_task(task_id).steps) == 3 * 2 + 1

    run(scenario())


# ----- cancel -----


def test_cancel_mid_way_posts_a_partial_summary_and_a_notice(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.providers[MODELS[1]].block_at = 1  # the second persona never answers

    async def scenario() -> None:
        conversation_id, task_id = await env.service.start(TOPIC, PRESETS, 2)
        stop = asyncio.Event()
        worker = asyncio.create_task(env.service.run_worker(stop))
        try:
            async with asyncio.timeout(10):
                while env.providers[MODELS[1]].calls < 1:
                    await asyncio.sleep(0.01)
                env.service.cancel(task_id)
                while env.repo.get_task(task_id).status not in FINAL:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.2)
        finally:
            stop.set()
            await worker
        assert env.repo.get_task(task_id).status is TaskStatus.CANCELLED
        messages = await env.messages(conversation_id)
        speakers = [parse_message(text).speaker for _, text in messages[1:]]
        assert speakers == ["設計者", "まとめ", "お知らせ"]
        assert notice_code(parse_message(messages[-1][1]).text) == "CANCELLED"
        # The summary was asked of the moderator from the turns so far, and no further turn ran.
        asked = env.default.requests[-1].messages[-1].content
        assert "設計者" in asked and "途中で停止" in asked
        assert env.providers[MODELS[2]].calls == 0

    run(scenario())


def test_cancel_before_any_turn_posts_only_the_notice(env: Env) -> None:
    async def scenario() -> None:
        conversation_id, task_id = await env.service.start(TOPIC, PRESETS, 1)
        env.service._loop_ref = asyncio.get_running_loop()
        env.service.cancel(task_id)  # never started
        await asyncio.sleep(0.1)
        messages = await env.messages(conversation_id)
        assert [parse_message(text).speaker for _, text in messages[1:]] == ["お知らせ"]
        assert env.repo.get_task(task_id).status is TaskStatus.CANCELLED

    run(scenario())


def test_cancel_is_for_discussion_tasks_only(env: Env) -> None:
    other = env.repo.create_task("something else", ["x"])
    with pytest.raises(Exception) as caught:
        env.service.cancel(other.id)
    assert type(caught.value).__name__ == "NotADiscussionTask"


# ----- failures -----


def test_three_provider_failures_stop_with_a_fixed_code_notice(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for fake in env.providers.values():
        fake.fail = True

    async def scenario() -> None:
        conversation_id, task_id = await env.service.start(TOPIC, PRESETS, 2)
        await env.run_until_final()
        task = env.repo.get_task(task_id)
        assert task.status is TaskStatus.FAILED
        messages = await env.messages(conversation_id)
        notices = [parse_message(t) for _, t in messages[1:]]
        assert all(n.speaker == "お知らせ" for n in notices)
        assert notice_code(notices[-1].text) == "PROVIDER_FAILURES"
        assert [notice_code(n.text) for n in notices[:-1]] == ["PERSONA_SKIPPED"] * 3
        # The engine's own notice is there once: the closing step adds nothing.
        assert sum(1 for n in notices if notice_code(n.text) == "PROVIDER_FAILURES") == 1
        assert all("PROVIDER-SECRET-DETAIL" not in text for _, text in messages)

    run(scenario())


# ----- limits -----


def test_per_day_limit_counts_todays_discussion_tasks(tmp_path: Path) -> None:
    now = [datetime.now(UTC)]
    env = Env(tmp_path, per_day_limit=2, clock=lambda: now[0])

    async def scenario() -> None:
        await env.service.start(TOPIC, PRESETS, 1)
        await env.run_until_final()
        await env.service.start(TOPIC, PRESETS, 1)
        await env.run_until_final()
        with pytest.raises(DiscussionError) as caught:
            await env.service.start(TOPIC, PRESETS, 1)
        assert str(caught.value) == "DISCUSSION_DAILY_LIMIT"
        config = env.service.config()
        assert (config["used_today"], config["remaining_today"]) == (2, 0)
        now[0] += timedelta(days=2)
        await env.service.start(TOPIC, PRESETS, 1)

    run(scenario())


def test_only_one_discussion_runs_at_a_time(env: Env) -> None:
    async def scenario() -> None:
        _, first = await env.service.start(TOPIC, PRESETS, 1)
        with pytest.raises(DiscussionError) as caught:
            await env.service.start(TOPIC, PRESETS, 1)
        assert str(caught.value) == "DISCUSSION_BUSY"
        assert env.service.config()["active"]["task_id"] == str(first)
        await env.run_until_final()
        assert env.service.config()["active"] is None
        await env.service.start(TOPIC, PRESETS, 1)  # free again

    run(scenario())


@pytest.mark.parametrize(
    ("personas", "code"),
    [
        (
            [
                {"name": "A", "role": "r", "model": "openai:other"},
                {"name": "B", "role": "r", "model": MODELS[0]},
            ],
            "PERSONA_MODEL_NOT_ALLOWED",
        ),
        (["設計者"], "DISCUSSION_PARTICIPANTS_INVALID"),
        (["設計者", "存在しない"], "DISCUSSION_PRESET_UNKNOWN"),
        (["設計者", 5], "PERSONA_INVALID"),
        ("設計者", "DISCUSSION_REQUEST_INVALID"),
        (
            [
                {"name": "まとめ", "role": "r", "model": MODELS[0]},
                {"name": "B", "role": "r", "model": MODELS[0]},
            ],
            "PERSONA_NAME_INVALID",
        ),
        (
            [
                {"name": "A", "role": "r", "model": MODELS[0], "extra": 1},
                {"name": "B", "role": "r", "model": MODELS[0]},
            ],
            "PERSONA_FIELD_UNKNOWN",
        ),
    ],
)
def test_bad_personas_are_refused_with_a_fixed_code(env: Env, personas, code: str) -> None:
    async def scenario() -> None:
        with pytest.raises(DiscussionError) as caught:
            await env.service.start(TOPIC, personas, 1)
        assert str(caught.value) == code
        assert env.repo.list_tasks() == []

    run(scenario())


def test_topic_and_unavailable_model_are_refused(tmp_path: Path) -> None:
    env = Env(tmp_path, ready=lambda provider: provider != "groq")

    async def scenario() -> None:
        for bad in ("", "   ", "x" * 2001, None, 5):
            with pytest.raises(DiscussionError) as caught:
                await env.service.start(bad, PRESETS, 1)
            assert str(caught.value) == "DISCUSSION_TOPIC_INVALID"
        own = [
            {"name": "A", "role": "r", "model": MODELS[2]},
            {"name": "B", "role": "r", "model": MODELS[0]},
        ]
        with pytest.raises(DiscussionError) as caught:
            await env.service.start(TOPIC, own, 1)
        assert str(caught.value) == "DISCUSSION_MODEL_UNAVAILABLE"
        # The presets only use models that are ready.
        presets = env.service.config()["presets"]
        assert all(not p["model"].startswith("groq:") for p in presets)

    run(scenario())


# ----- the task queue -----


def test_discussion_tasks_are_claimed_only_by_the_discussion_worker(env: Env) -> None:
    repo = env.repo
    repo.create_task(chat_tool_goal(UUID(int=1), "a" * 32), ["x"])
    repo.create_task(discussion_goal(UUID(int=2), "b" * 32), ["x"])
    assert repo.claim_next() is None
    chat = repo.claim_next(CHAT_TOOL_GOAL_PREFIX)
    assert chat is not None and chat.goal.startswith(CHAT_TOOL_GOAL_PREFIX)
    assert repo.claim_next(CHAT_TOOL_GOAL_PREFIX) is None
    mine = repo.claim_next(DISCUSSION_GOAL_PREFIX)
    assert mine is not None and mine.goal.startswith(DISCUSSION_GOAL_PREFIX)
    plain = repo.create_task("web research", ["x"])
    assert repo.claim_next().id == plain.id


def test_recover_settles_a_running_discussion_with_a_notice(env: Env) -> None:
    async def scenario() -> None:
        conversation_id, task_id = await env.service.start(TOPIC, PRESETS, 1)
        env.repo.claim_next(DISCUSSION_GOAL_PREFIX)
        assert await env.service.recover() == 1
        task = env.repo.get_task(task_id)
        assert task.status is TaskStatus.FAILED and task.failure_code.value == "interrupted"
        messages = await env.messages(conversation_id)
        assert notice_code(parse_message(messages[-1][1]).text) == "INTERRUPTED"

    run(scenario())


def test_no_topic_prompt_or_turn_text_reaches_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = Env(tmp_path)
    env.providers[MODELS[1]].fail = True
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        _, task_id = await env.service.start(TOPIC, PRESETS, 1)
        await env.run_until_final()
        _, second = await env.service.start(TOPIC, PRESETS, 1)
        env.service._loop_ref = asyncio.get_running_loop()
        env.service.cancel(second)
        await asyncio.sleep(0.1)
        assert env.repo.get_task(task_id).status in FINAL

    run(scenario())
    text = "\n".join(
        record.getMessage() + " " + str(sorted(record.__dict__.items(), key=str))
        for record in caplog.records
    )
    for secret in ("TOPIC-SENTINEL", "reply-", "PROVIDER-SECRET-DETAIL", "stance", "批判役"):
        assert secret not in text
    assert "discussion.queued" in text


def test_the_schema_version_is_unchanged() -> None:
    assert SCHEMA_VERSION == 10


# ----- the API -----


def make_app(tmp_path: Path, **overrides):
    env = Env(tmp_path)
    settings = Settings(
        db_path=tmp_path / "db.sqlite3",
        discussion=True,
        model_choices=MODELS,
        **overrides,
    )
    app = create_app(settings, env.default, models=env.registry)
    return env, settings, app


def wait_for(predicate: Callable[[], bool], limit: float = 10.0) -> None:
    deadline = time.monotonic() + limit
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_api_end_to_end(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        config = client.get("/api/discussions/config").json()
        assert config["enabled"] is True
        assert (config["max_rounds"], config["per_day_limit"]) == (3, 10)
        assert [m["id"] for m in config["models"]] == list(MODELS)
        assert [p["name"] for p in config["presets"]] == PRESETS
        body = {"topic": TOPIC, "personas": PRESETS, "rounds": 1}
        response = client.post("/api/discussions", json=body, headers=HEADERS)
        assert response.status_code == 201
        ids = response.json()
        assert set(ids) == {"conversation_id", "task_id"}
        wait_for(lambda: client.get(f"/api/tasks/{ids['task_id']}").json()["status"] in (
            "completed", "failed", "cancelled"
        ))
        task = client.get(f"/api/tasks/{ids['task_id']}").json()
        assert task["status"] == "completed" and task["conversation_id"] == ids["conversation_id"]
        url = f"/api/chat/conversations/{ids['conversation_id']}/messages"
        read = client.get(url).json()
        assert read["total"] == 5
        assert [parse_message(m["content"]).speaker for m in read["messages"][1:]] == [
            *PRESETS,
            "まとめ",
        ]
        after = client.get(url, params={"after": 3}).json()
        assert len(after["messages"]) == 2
        # A second one is possible once the first is done, and the config reflects usage.
        assert client.get("/api/discussions/config").json()["used_today"] == 1


def test_api_guards_and_fixed_error_codes(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    body = {"topic": TOPIC, "personas": PRESETS, "rounds": 1}
    with TestClient(app) as client:
        assert client.post("/api/discussions", json=body).status_code == 403  # no confirm header
        wrong = client.post("/api/discussions", json=body, headers={"X-Jarvis-Confirm": "yes"})
        assert wrong.status_code == 403
        cross = client.post(
            "/api/discussions", json=body, headers={**HEADERS, "Origin": "https://evil.example"}
        )
        assert cross.status_code == 403
        same = client.post(
            "/api/discussions", json=body, headers={**HEADERS, "Origin": "http://testserver"}
        )
        assert same.status_code == 201


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"topic": "", "personas": PRESETS}, 400, "DISCUSSION_TOPIC_INVALID"),
        ({"topic": "t", "personas": ["設計者"]}, 400, "DISCUSSION_PARTICIPANTS_INVALID"),
        ({"topic": "t", "personas": PRESETS, "rounds": 0}, 400, "DISCUSSION_ROUNDS_INVALID"),
        ({"topic": "t", "personas": PRESETS, "extra": 1}, 400, "DISCUSSION_REQUEST_INVALID"),
        ({"topic": "t"}, 400, "DISCUSSION_REQUEST_INVALID"),
        (
            {
                "topic": "t",
                "personas": [
                    {"name": "A", "role": "r", "model": "openai:evil-model"},
                    {"name": "B", "role": "r", "model": MODELS[0]},
                ],
            },
            400,
            "PERSONA_MODEL_NOT_ALLOWED",
        ),
    ],
)
def test_api_errors_are_fixed_codes_that_never_echo_the_input(
    tmp_path: Path, payload, status: int, code: str
) -> None:
    _, _, app = make_app(tmp_path)
    payload = {**payload}
    with TestClient(app) as client:
        response = client.post("/api/discussions", json=payload, headers=HEADERS)
        assert response.status_code == status
        assert response.json() == {"detail": code}
        garbage = client.post(
            "/api/discussions", content=b"{not json SECRET-INPUT", headers=HEADERS
        )
        assert garbage.status_code == 400 and "SECRET-INPUT" not in garbage.text
        huge = client.post("/api/discussions", content=b"{" + b" " * 70_000, headers=HEADERS)
        assert huge.status_code == 400
        assert huge.json() == {"detail": "DISCUSSION_REQUEST_INVALID"}


def test_api_busy_and_daily_limit_codes(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path, discussion_per_day_limit=1)
    body = {"topic": TOPIC, "personas": PRESETS, "rounds": 1}
    with TestClient(app) as client:
        assert client.post("/api/discussions", json=body, headers=HEADERS).status_code == 201
        second = client.post("/api/discussions", json=body, headers=HEADERS)
        assert second.status_code in (409, 429)
        assert second.json()["detail"] in ("DISCUSSION_BUSY", "DISCUSSION_DAILY_LIMIT")
        wait_for(lambda: client.get("/api/discussions/config").json()["active"] is None)
        third = client.post("/api/discussions", json=body, headers=HEADERS)
        assert (third.status_code, third.json()) == (429, {"detail": "DISCUSSION_DAILY_LIMIT"})


def test_api_cancel_goes_through_the_task_route(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    env.providers[MODELS[0]].block_at = 1  # the first persona never answers
    body = {"topic": TOPIC, "personas": PRESETS, "rounds": 1}
    with TestClient(app) as client:
        ids = client.post("/api/discussions", json=body, headers=HEADERS).json()
        task_url = f"/api/tasks/{ids['task_id']}"
        wait_for(lambda: client.get(task_url).json()["status"] == "running")
        assert client.post(f"{task_url}/cancel").status_code == 403  # no confirm header
        cancel = client.post(f"{task_url}/cancel", headers=HEADERS)
        assert cancel.status_code == 200
        wait_for(lambda: client.get(task_url).json()["status"] == "cancelled")
        url = f"/api/chat/conversations/{ids['conversation_id']}/messages"
        wait_for(lambda: client.get(url).json()["total"] >= 2)
        last = client.get(url).json()["messages"][-1]["content"]
        assert notice_code(parse_message(last).text) == "CANCELLED"
        again = client.post(f"{task_url}/cancel", headers=HEADERS)
        assert again.status_code == 409
        other = TaskRepository(env.db).create_task("something else", ["x"])
        assert client.post(f"/api/tasks/{other.id}/cancel", headers=HEADERS).status_code == 404


def test_feature_off_has_no_routes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    settings = Settings(db_path=tmp_path / "db.sqlite3", model_choices=MODELS)
    with TestClient(create_app(settings, env.default, models=env.registry)) as client:
        assert client.get("/api/discussions/config").status_code == 404
        assert client.post("/api/discussions", json={}, headers=HEADERS).status_code in (404, 405)
        other = TaskRepository(env.db).create_task("something else", ["x"])
        assert client.post(f"/api/tasks/{other.id}/cancel", headers=HEADERS).status_code == 404


def test_feature_on_without_the_model_allowlist_has_no_routes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    settings = Settings(db_path=tmp_path / "db.sqlite3", discussion=True)
    with TestClient(create_app(settings, env.default)) as client:
        assert client.get("/api/discussions/config").status_code == 404


def test_auth_is_required_for_the_discussion_routes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    passphrase = "fake passphrase for tests only"
    settings = Settings(
        db_path=tmp_path / "db.sqlite3",
        discussion=True,
        model_choices=MODELS,
        auth_passphrase_hash=hash_passphrase(passphrase, n=16, r=1, p=1),
        auth_signing_key="k" * 40,
        auth_cookie_secure=False,
    )
    auth = AuthService.from_settings(settings, limiter=LoginLimiter(failure_delay=0))
    app = create_app(settings, env.default, models=env.registry, auth=auth)
    with TestClient(app, base_url="http://testserver") as client:
        body = {"topic": TOPIC, "personas": PRESETS, "rounds": 1}
        assert client.get("/api/discussions/config").status_code == 401
        assert client.post("/api/discussions", json=body, headers=HEADERS).status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"passphrase": passphrase},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        assert client.get("/api/discussions/config").status_code == 200
        origin = {**HEADERS, "Origin": "http://testserver"}
        assert client.post("/api/discussions", json=body, headers=origin).status_code == 201
