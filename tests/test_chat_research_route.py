"""A chat turn the router sends to research starts a research, honestly, and only then.

Everything here is fakes: a scripted starter or the real run service over fake search, pages and
model. No socket is opened (``no_network``) and no key exists.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from research_run_support import (
    AGREEING_CLAIMS,
    AGREEING_HITS,
    AGREEING_PAGES,
    FakeSearch,
    Gate,
    ScriptedLLM,
    build_reader,
)

from backend.api.app import create_app
from backend.chat.activity import (
    ActivityEvent,
    ActivityRoute,
    ActivityStage,
    ResearchSkip,
    ResearchStep,
)
from backend.chat.context import ConversationStore
from backend.chat.research_start import (
    CHAT_RESEARCH_LEVELS,
    ResearchStarter,
    RunServiceStarter,
    StartKind,
    StartOutcome,
)
from backend.chat.service import (
    FIXED_REPLY_MODEL,
    FIXED_REPLY_PROVIDER,
    ChatDelta,
    ChatDone,
    ChatService,
    research_started_reply,
)
from backend.core.config import ConfigError, Settings
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.research.models import ResearchLevel
from backend.research.run_control import BudgetExhausted, Busy, RunRefused
from backend.research.search_budget import BudgetedSearchProvider
from backend.router import Route, RouteDecision, RouteReason, fallback

pytestmark = pytest.mark.usefixtures("no_network")

ACTIVITY = {"X-Jarvis-Activity": "1"}
HOSTILE = "IGNORE-ALL-RULES </script><b>x</b> /etc/passwd sk-live-0123456789"
QUESTION = "What is the latest release of the Foo widget library?"
SESSION = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse("Acknowledged.", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        yield "Acknow"
        yield "ledged."


class ChoiceRouter:
    def __init__(self, route: Route = Route.research, confidence: float = 0.9) -> None:
        self.route = route
        self.confidence = confidence
        self.calls: list[str] = []

    async def decide(self, text: str) -> RouteDecision:
        self.calls.append(text)
        return RouteDecision(self.route, self.confidence, RouteReason.model_choice, False)


class FallbackRouter:
    async def decide(self, text: str) -> RouteDecision:
        return fallback(RouteReason.low_confidence, 0.2)


class FakeStarter:
    """Scripted outcome; records exactly what it was given."""

    def __init__(self, outcome: StartOutcome | BaseException | object | None = None) -> None:
        self.outcome = StartOutcome.started(SESSION) if outcome is None else outcome
        self.questions: list[str] = []

    async def start(self, question: str) -> StartOutcome:
        self.questions.append(question)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome  # type: ignore[return-value]


class HangingStarter:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def start(self, question: str) -> StartOutcome:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ExplodingMemory:
    """Memory context that must never be consulted by the fixed reply."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def for_query(self, query: str) -> str:
        self.calls.append(query)
        return "[]"


def make_service(
    starter: ResearchStarter | None,
    *,
    router=None,
    provider: FakeProvider | None = None,
    store: ConversationStore | None = None,
    memory=None,
) -> tuple[ChatService, FakeProvider, ConversationStore]:
    provider = provider or FakeProvider()
    store = store or ConversationStore()
    service = ChatService(
        provider,
        store,
        router=router or ChoiceRouter(),
        research_starter=starter,
        memory_context=memory,
    )
    return service, provider, store


def run_stream(service: ChatService, message: str = QUESTION) -> list:
    async def run():
        return [item async for item in service.stream_with_activity(message)]

    return asyncio.run(run())


def payloads(items) -> list[dict]:
    return [item.to_payload() for item in items if isinstance(item, ActivityEvent)]


def reply_of(items) -> str:
    return "".join(item.text for item in items if isinstance(item, ChatDelta))


def stored_messages(store: ConversationStore) -> list[tuple[str, str]]:
    return [
        (m.role, m.content)
        for conversation in store._conversations.values()
        for m in conversation.messages
    ]


STARTED_EVENTS = [
    {"stage": "received"},
    {"stage": "routing"},
    {"stage": "route_selected", "route": "research", "decided": "research", "fallback": False},
    {"stage": "researching", "step": "started"},
    {"stage": "done"},
]


# --- a started research --------------------------------------------------------------------


def test_a_research_decision_starts_a_research_and_replies_with_the_fixed_text() -> None:
    starter = FakeStarter()
    memory = ExplodingMemory()
    service, provider, store = make_service(starter, memory=memory)
    items = run_stream(service)
    assert payloads(items) == STARTED_EVENTS
    reply = reply_of(items)
    assert reply == research_started_reply(SESSION)
    assert f"/research#{SESSION}" in reply
    assert "『リサーチ』" in reply and "調査を開始しました" in reply
    assert isinstance(items[-1], ChatDone)
    assert (items[-1].provider, items[-1].model) == (FIXED_REPLY_PROVIDER, FIXED_REPLY_MODEL)
    # No model call for the reply, no memory, no history; the starter got the message only.
    assert provider.requests == []
    assert memory.calls == []
    assert starter.questions == [QUESTION]
    # Saved like a normal assistant turn.
    assert stored_messages(store) == [("user", QUESTION), ("assistant", reply)]


def test_the_fixed_reply_is_part_of_the_history_of_the_next_turn() -> None:
    service, provider, _ = make_service(FakeStarter())

    async def run():
        first = await service.complete(QUESTION)
        # A second turn decided as memory sees the fixed reply as history, nothing else changed.
        service.router = ChoiceRouter(Route.memory)
        await service.complete("thanks", first.conversation_id)
        return first

    first = asyncio.run(run())
    history = [m.content for m in provider.requests[0].messages]
    assert QUESTION in history and first.reply in history
    assert (first.provider, first.model) == (FIXED_REPLY_PROVIDER, FIXED_REPLY_MODEL)


def test_complete_reports_the_same_stages_and_returns_the_fixed_reply() -> None:
    service, provider, store = make_service(FakeStarter())
    observed: list[ActivityEvent] = []
    result = asyncio.run(service.complete(QUESTION, on_activity=observed.append))
    assert [e.to_payload() for e in observed] == STARTED_EVENTS
    assert result.reply == research_started_reply(SESSION)
    assert provider.requests == []
    assert stored_messages(store)[-1] == ("assistant", result.reply)


def test_the_plain_stream_has_the_fixed_reply_as_one_delta() -> None:
    service, provider, _ = make_service(FakeStarter())

    async def run():
        return [item async for item in service.stream(QUESTION)]

    items = asyncio.run(run())
    assert [type(item) for item in items] == [ChatDelta, ChatDone]
    assert items[0].text == research_started_reply(SESSION)
    assert provider.requests == []


# --- every other outcome: the Main Agent answers and the event says why --------------------

SKIPS = {
    StartKind.BUSY: ResearchSkip.BUSY,
    StartKind.NOT_CONFIGURED: ResearchSkip.NOT_CONFIGURED,
    StartKind.BUDGET_EXHAUSTED: ResearchSkip.BUDGET_EXHAUSTED,
    StartKind.REFUSED: ResearchSkip.REFUSED,
}


@pytest.mark.parametrize("kind", list(SKIPS), ids=[k.value for k in SKIPS])
def test_a_start_that_does_not_happen_falls_back_to_the_main_agent(kind: StartKind) -> None:
    starter = FakeStarter(StartOutcome(kind))
    service, provider, store = make_service(starter)
    items = run_stream(service)
    assert payloads(items) == [
        {"stage": "received"},
        {"stage": "routing"},
        {
            "stage": "route_selected",
            "route": "main",
            "decided": "research",
            "fallback": False,
            "research_skip": SKIPS[kind].value,
        },
        {"stage": "generating"},
        {"stage": "done"},
    ]
    assert reply_of(items) == "Acknowledged."  # the model's answer, not a made-up one
    assert len(provider.requests) == 1
    assert starter.questions == [QUESTION]
    assert stored_messages(store) == [("user", QUESTION), ("assistant", "Acknowledged.")]


def test_a_starter_that_raises_or_answers_nonsense_counts_as_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    for outcome in (
        RuntimeError(f"broke: {HOSTILE}"),
        object(),  # not an outcome
        "started",
        {"kind": "started"},
    ):
        caplog.clear()
        service, provider, _ = make_service(FakeStarter(outcome))
        with caplog.at_level(logging.DEBUG):
            items = run_stream(service, HOSTILE)
        selected = payloads(items)[2]
        assert selected["route"] == "main" and selected["research_skip"] == "refused"
        assert reply_of(items) == "Acknowledged."
        assert len(provider.requests) == 1
        assert HOSTILE not in caplog.text and "sk-live" not in caplog.text
    assert "chat.research_start_failed" in caplog.text


def test_no_research_for_a_router_fallback_or_other_routes() -> None:
    cases = [
        (FallbackRouter(), "memory", True),
        (ChoiceRouter(Route.memory), "memory", False),
        (ChoiceRouter(Route.casual), "casual", False),
    ]
    for router, decided, was_fallback in cases:
        starter = FakeStarter()
        service, provider, _ = make_service(starter, router=router)
        items = run_stream(service)
        assert starter.questions == []
        assert payloads(items)[2] == {
            "stage": "route_selected",
            "route": "main",
            "decided": decided,
            "fallback": was_fallback,
        }
        assert reply_of(items) == "Acknowledged."
        assert len(provider.requests) == 1


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.59])
def test_a_low_confidence_research_decision_never_starts_one(confidence: float) -> None:
    starter = FakeStarter()
    service, provider, _ = make_service(starter, router=ChoiceRouter(Route.research, confidence))
    items = run_stream(service)
    assert starter.questions == []
    assert payloads(items)[2]["research_skip"] == "low_confidence"
    assert payloads(items)[2]["route"] == "main"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("make_router", [lambda: ChoiceRouter(), None], ids=["router", "no-router"])
def test_without_a_starter_a_research_decision_is_unchanged(make_router) -> None:
    provider = FakeProvider()
    service = ChatService(
        provider,
        ConversationStore(),
        router=make_router() if make_router else None,
    )
    items = run_stream(service)
    stages = payloads(items)
    assert reply_of(items) == "Acknowledged."
    if make_router:
        # Exactly what the router-only slice emitted: no skip field, the unwired wording applies.
        assert stages[2] == {
            "stage": "route_selected",
            "route": "main",
            "decided": "research",
            "fallback": False,
        }
    else:
        assert [p["stage"] for p in stages] == ["received", "generating", "done"]
    assert all("research_skip" not in p for p in stages)
    assert len(provider.requests) == 1


def test_a_starter_without_a_router_is_never_asked() -> None:
    starter = FakeStarter()
    service = ChatService(FakeProvider(), ConversationStore(), research_starter=starter)
    items = run_stream(service)
    assert starter.questions == []
    assert [p["stage"] for p in payloads(items)] == ["received", "generating", "done"]


# --- cancellation, hostile text ------------------------------------------------------------


def test_cancellation_while_starting_propagates_and_saves_nothing() -> None:
    async def run():
        hanging = HangingStarter()
        service, provider, store = make_service(hanging)
        seen: list[ActivityEvent] = []

        async def consume():
            async for item in service.stream_with_activity(QUESTION):
                if isinstance(item, ActivityEvent):
                    seen.append(item)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(hanging.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return seen, provider, store

    seen, provider, store = asyncio.run(run())
    assert [e.stage for e in seen] == [ActivityStage.RECEIVED, ActivityStage.ROUTING]
    assert provider.requests == []
    assert stored_messages(store) == []


def test_a_cancelled_starter_is_not_a_refusal() -> None:
    service, provider, store = make_service(FakeStarter(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.complete(QUESTION))
    assert provider.requests == []
    assert stored_messages(store) == []


def test_hostile_message_text_stays_out_of_events_logs_and_the_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    starter = FakeStarter()
    service, provider, _ = make_service(starter)
    with caplog.at_level(logging.DEBUG):
        items = run_stream(service, HOSTILE)
    assert starter.questions == [HOSTILE]  # the starter gets the message, as the privacy note says
    assert payloads(items) == STARTED_EVENTS
    assert HOSTILE not in json.dumps(payloads(items))
    assert HOSTILE not in reply_of(items)
    assert HOSTILE not in caplog.text and "sk-live" not in caplog.text
    assert "chat.research_started" in caplog.text
    assert provider.requests == []


# --- the starter over the run service ------------------------------------------------------


class FakeRuns:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, ResearchLevel]] = []

    def submit(self, question: str, level: ResearchLevel) -> UUID:
        self.calls.append((question, level))
        if self.error is not None:
            raise self.error
        return SESSION

    def cancel(self, session_id: UUID) -> str:  # pragma: no cover - not used
        raise NotImplementedError

    def progress(self, session_id: UUID):  # pragma: no cover - not used
        return None


def test_the_run_service_starter_hands_over_the_question_and_the_level_only() -> None:
    runs = FakeRuns()
    outcome = asyncio.run(RunServiceStarter(runs).start(QUESTION))
    assert outcome == StartOutcome(StartKind.STARTED, SESSION)
    assert runs.calls == [(QUESTION, ResearchLevel.QUICK)]
    runs = FakeRuns()
    asyncio.run(RunServiceStarter(runs, ResearchLevel.STANDARD).start(QUESTION))
    assert runs.calls == [(QUESTION, ResearchLevel.STANDARD)]


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (Busy(), StartKind.BUSY),
        (BudgetExhausted(), StartKind.BUDGET_EXHAUSTED),
        (RunRefused(), StartKind.REFUSED),
        (ValueError("bad question"), StartKind.REFUSED),
        (RuntimeError(HOSTILE), StartKind.REFUSED),
    ],
)
def test_the_run_service_starter_maps_every_refusal_to_a_fixed_outcome(
    error: BaseException, kind: StartKind, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        outcome = asyncio.run(RunServiceStarter(FakeRuns(error)).start(QUESTION))
    assert outcome == StartOutcome(kind)
    assert HOSTILE not in caplog.text


def test_the_run_service_starter_refuses_unacceptable_questions_before_submitting() -> None:
    runs = FakeRuns()
    starter = RunServiceStarter(runs)
    for question in ("", "   ", "x" * 2001, "bad\x00text", "esc\x1b[0m", "del\x7f"):
        assert asyncio.run(starter.start(question)) == StartOutcome(StartKind.REFUSED)
    assert runs.calls == []
    assert asyncio.run(starter.start("two\nlines\tand tab")).kind is StartKind.STARTED


def test_the_run_service_starter_without_a_service_is_not_configured() -> None:
    assert asyncio.run(RunServiceStarter(None).start(QUESTION)) == StartOutcome(
        StartKind.NOT_CONFIGURED
    )
    with pytest.raises(ValueError):
        RunServiceStarter(FakeRuns(), ResearchLevel.DEEP)


def test_start_outcomes_are_consistent() -> None:
    with pytest.raises(ValueError):
        StartOutcome(StartKind.STARTED)
    with pytest.raises(ValueError):
        StartOutcome(StartKind.BUSY, SESSION)
    with pytest.raises(ValueError):
        StartOutcome("started", SESSION)  # type: ignore[arg-type]
    assert CHAT_RESEARCH_LEVELS == ("quick", "standard")


# --- the event type ------------------------------------------------------------------------


def test_activity_event_rules_for_the_research_route() -> None:
    started = ActivityEvent.route_selected(ActivityRoute.RESEARCH, ActivityRoute.RESEARCH, False)
    assert started.to_payload() == STARTED_EVENTS[2]
    skipped = ActivityEvent.route_selected(
        ActivityRoute.MAIN, ActivityRoute.RESEARCH, False, ResearchSkip.BUSY
    )
    assert skipped.to_payload()["research_skip"] == "busy"
    assert ActivityEvent.researching(ResearchStep.STARTED).to_payload() == {
        "stage": "researching",
        "step": "started",
    }
    for build in (
        # research ran without a research decision, or on a fallback
        lambda: ActivityEvent.route_selected(
            ActivityRoute.RESEARCH, ActivityRoute.MEMORY, False
        ),
        lambda: ActivityEvent.route_selected(ActivityRoute.RESEARCH, ActivityRoute.RESEARCH, True),
        # a skip only belongs to a research decision that ran on main
        lambda: ActivityEvent.route_selected(
            ActivityRoute.MAIN, ActivityRoute.MEMORY, False, ResearchSkip.BUSY
        ),
        lambda: ActivityEvent.route_selected(
            ActivityRoute.RESEARCH, ActivityRoute.RESEARCH, False, ResearchSkip.BUSY
        ),
        lambda: ActivityEvent.route_selected(
            ActivityRoute.MAIN, ActivityRoute.RESEARCH, True, ResearchSkip.BUSY
        ),
        lambda: ActivityEvent.route_selected(
            ActivityRoute.MAIN, ActivityRoute.RESEARCH, False, "busy"  # type: ignore[arg-type]
        ),
        lambda: ActivityEvent(ActivityStage.DONE, research_skip=ResearchSkip.BUSY),
    ):
        with pytest.raises(ValueError):
            build()


# --- over HTTP, with the real run service and fakes ----------------------------------------


class CombinedProvider:
    """One provider for chat AND research, as in production. Research prompts (they carry
    evidence blocks) go to the scripted research model; everything else is a chat turn."""

    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.chat = FakeProvider()
        self.research = ScriptedLLM(AGREEING_CLAIMS)

    @property
    def requests(self) -> list[CompletionRequest]:
        return self.chat.requests

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if any("<evidence" in m.content for m in request.messages):
            return await self.research.complete(request)
        return await self.chat.complete(request)

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        return self.chat.stream(request)


def make_client(
    tmp_path: Path,
    *,
    router=None,
    search="default",
    name: str = "chat-research",
    **overrides,
) -> tuple[TestClient, CombinedProvider, FakeSearch]:
    provider = CombinedProvider()
    fake_search = FakeSearch(AGREEING_HITS) if search == "default" else search
    reader, _ = build_reader(AGREEING_PAGES)
    overrides.setdefault("research_enabled", True)
    app = create_app(
        Settings(db_path=tmp_path / f"{name}.sqlite3", **overrides),
        provider,
        search_provider=fake_search,
        page_reader=reader,
        router=router if router is not None else ChoiceRouter(),
    )
    return TestClient(app), provider, fake_search


def sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def wait_status(client: TestClient, session_id: str, wanted: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/research/sessions/{session_id}").json()
        if detail["status"] == wanted:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"session never became {wanted}")


def session_id_from(reply: str) -> str:
    marker = "/research#"
    start = reply.index(marker) + len(marker)
    return reply[start : start + 36]


def test_the_sse_endpoint_starts_a_real_research_from_the_message_only(tmp_path: Path) -> None:
    message = f"{QUESTION} {HOSTILE}"
    router = ChoiceRouter()
    client, provider, search = make_client(tmp_path, router=router)
    with client:
        response = client.post("/api/chat/stream", json={"message": message}, headers=ACTIVITY)
        events = sse(response.text)
        assert [name for name, _ in events] == [
            "activity", "activity", "activity", "activity", "delta", "activity", "done",
        ]
        assert [data for name, data in events if name == "activity"] == STARTED_EVENTS
        delta = next(data for name, data in events if name == "delta")["text"]
        done = events[-1][1]
        assert (done["provider"], done["model"]) == (FIXED_REPLY_PROVIDER, FIXED_REPLY_MODEL)
        session_id = session_id_from(delta)
        assert delta == research_started_reply(UUID(session_id))
        detail = wait_status(client, session_id, "completed")
        # The question is the user's message and nothing else: no history, no memory.
        assert detail["question"] == message
        assert detail["level"] == "quick"
        assert provider.chat.requests == []  # no model call for the fixed reply
        assert search.calls and all("Acknowledged" not in q for q in search.calls)
        # The conversation was saved with the fixed reply: the next turn sees it as history.
        router.route = Route.memory
        second = client.post(
            "/api/chat",
            json={"message": "and now?", "conversation_id": done["conversation_id"]},
        )
        assert second.status_code == 200
    seen = [m.content for m in provider.chat.requests[0].messages]
    assert message in seen and delta in seen


def test_the_stream_without_the_header_and_the_json_endpoint_stay_compatible(
    tmp_path: Path,
) -> None:
    client, provider, _ = make_client(tmp_path)
    with client:
        plain = client.post("/api/chat/stream", json={"message": QUESTION})
        assert [name for name, _ in sse(plain.text)] == ["delta", "done"]
        assert "activity" not in plain.text
        wait_status(client, session_id_from(sse(plain.text)[0][1]["text"]), "completed")
        reply = client.post("/api/chat", json={"message": "second question"})
    assert reply.status_code == 200
    body = reply.json()
    assert set(body) == {"conversation_id", "reply", "provider", "model"}
    assert (body["provider"], body["model"]) == (FIXED_REPLY_PROVIDER, FIXED_REPLY_MODEL)
    assert "/research#" in body["reply"]
    assert provider.chat.requests == []


def test_a_second_message_while_a_research_runs_is_answered_by_the_main_agent(
    tmp_path: Path,
) -> None:
    gate = Gate()
    client, provider, _ = make_client(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate))
    with client:
        first = client.post("/api/chat", json={"message": QUESTION}).json()
        assert gate.wait_until_entered()
        text = client.post(
            "/api/chat/stream", json={"message": "another research?"}, headers=ACTIVITY
        ).text
        selected = [d for n, d in sse(text) if n == "activity" and d["stage"] == "route_selected"]
        assert selected == [
            {
                "stage": "route_selected",
                "route": "main",
                "decided": "research",
                "fallback": False,
                "research_skip": "busy",
            }
        ]
        assert "Acknow" in text  # the Main Agent answered
        assert len(provider.chat.requests) == 1
        sessions = client.get("/api/research/sessions").json()["sessions"]
        assert [s["id"] for s in sessions] == [session_id_from(first["reply"])]
        gate.release()
        wait_status(client, session_id_from(first["reply"]), "completed")


def test_an_exhausted_budget_falls_back_to_the_main_agent(tmp_path: Path) -> None:
    inner = FakeSearch(AGREEING_HITS)
    guarded = BudgetedSearchProvider(inner, monthly_limit=3, usage_counter=lambda: 3)
    client, provider, _ = make_client(tmp_path, search=guarded)
    with client:
        text = client.post("/api/chat/stream", json={"message": QUESTION}, headers=ACTIVITY).text
        selected = next(d for n, d in sse(text) if d.get("stage") == "route_selected")
        assert selected["research_skip"] == "budget_exhausted" and selected["route"] == "main"
        assert client.get("/api/research/sessions").json() == {"sessions": []}
    assert inner.calls == [] and len(provider.chat.requests) == 1


@pytest.mark.parametrize(
    "case",
    [{"research_enabled": False}, {"search": None}],
    ids=["switch-off", "no-search-provider"],
)
def test_research_off_or_incomplete_leaves_the_router_slice_unchanged(
    tmp_path: Path, case: dict
) -> None:
    client, provider, _ = make_client(tmp_path, **case)
    with client:
        text = client.post("/api/chat/stream", json={"message": QUESTION}, headers=ACTIVITY).text
        selected = next(d for n, d in sse(text) if d.get("stage") == "route_selected")
        assert selected == {
            "stage": "route_selected",
            "route": "main",
            "decided": "research",
            "fallback": False,
        }
        assert "researching" not in text and "research_skip" not in text
        assert "Acknow" in text and len(provider.chat.requests) == 1
        assert client.get("/api/research/sessions").json() == {"sessions": []}


def test_no_router_means_no_research_from_chat(tmp_path: Path) -> None:
    provider = CombinedProvider()
    reader, _ = build_reader(AGREEING_PAGES)
    search = FakeSearch(AGREEING_HITS)
    app = create_app(
        Settings(db_path=tmp_path / "norouter.sqlite3", research_enabled=True),
        provider,
        search_provider=search,
        page_reader=reader,
    )
    with TestClient(app) as client:
        text = client.post("/api/chat/stream", json={"message": QUESTION}, headers=ACTIVITY).text
        assert "route_selected" not in text and "researching" not in text
        assert len(provider.chat.requests) == 1
        assert client.get("/api/research/sessions").json() == {"sessions": []}
    assert search.calls == []


def test_the_chat_research_level_is_a_fixed_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings(db_path=tmp_path / "x.sqlite3").chat_research_level == "quick"
    client, _, _ = make_client(tmp_path, chat_research_level="standard")
    with client:
        reply = client.post("/api/chat", json={"message": QUESTION}).json()["reply"]
        assert wait_status(client, session_id_from(reply), "completed")["level"] == "standard"
    monkeypatch.setenv("JARVIS_DB_PATH", "test.sqlite3")
    monkeypatch.delenv("JARVIS_CHAT_RESEARCH_LEVEL", raising=False)
    assert Settings.from_env().chat_research_level == "quick"
    for raw, expected in (("quick", "quick"), (" Standard ", "standard")):
        monkeypatch.setenv("JARVIS_CHAT_RESEARCH_LEVEL", raw)
        assert Settings.from_env().chat_research_level == expected
    for bad in ("deep", "extensive", "memory", "", "  ", "fast"):
        monkeypatch.setenv("JARVIS_CHAT_RESEARCH_LEVEL", bad)
        with pytest.raises(ConfigError, match="JARVIS_CHAT_RESEARCH_LEVEL"):
            Settings.from_env()
    with pytest.raises(ConfigError, match="JARVIS_CHAT_RESEARCH_LEVEL"):
        Settings(db_path=Path("x.sqlite3"), chat_research_level="deep")


def test_the_reply_carries_the_canonical_session_path_once() -> None:
    reply = research_started_reply(uuid4())
    assert reply.count("/research#") == 1
