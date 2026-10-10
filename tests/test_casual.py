"""The casual path: personality and the last turns only, with every failure on the Main Agent.

Fakes only. Spies stand in for memory and the research starter; they must never be called for a
casual turn.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend.api.app import create_app
from backend.chat.activity import (
    ActivityEvent,
    ActivityRoute,
    ActivityStage,
    CasualSkip,
)
from backend.chat.casual import (
    CASUAL_HISTORY_TURNS,
    CASUAL_MAX_OUTPUT_CHARS,
    CASUAL_RULES,
    CasualService,
)
from backend.chat.context import ConversationStore
from backend.chat.service import ChatDelta, ChatDone, ChatService
from backend.core.config import ConfigError, Settings
from backend.personality.prompt import SYSTEM_PROMPT
from backend.providers.base import (
    CompletionRequest,
    CompletionResponse,
    ProviderError,
)
from backend.router import Route, RouteDecision, RouteReason, fallback

pytestmark = pytest.mark.usefixtures("no_network")

ACTIVITY = {"X-Jarvis-Activity": "1"}
HELLO = "Good morning, how are you?"


class FakeProvider:
    """A scripted chat provider. ``mode`` chooses how the next calls behave."""

    def __init__(self, name: str = "fake", model: str = "fake-model") -> None:
        self.name = name
        self.model = model
        self.requests: list[CompletionRequest] = []
        self.deltas: tuple[str, ...] = ("Hel", "lo.")
        self.mode = "ok"  # ok | error | empty | boom | late_error

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.mode == "error":
            raise ProviderError("upstream said: secret-detail")
        if self.mode == "boom":
            raise RuntimeError("anything")
        text = "" if self.mode == "empty" else "".join(self.deltas)
        return CompletionResponse(text, self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests.append(request)
        if self.mode == "error":
            raise ProviderError("upstream said: secret-detail")
        if self.mode == "boom":
            raise RuntimeError("anything")
        if self.mode == "empty":
            return
        for index, delta in enumerate(self.deltas):
            yield delta
            if self.mode == "late_error" and index == 0:
                raise ProviderError("dropped")


class MainProvider(FakeProvider):
    """Used when the Main Agent should answer: the text is different from the casual one."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas = ("Main ", "answer.")


class ChoiceRouter:
    def __init__(self, route: Route = Route.casual, confidence: float = 0.9) -> None:
        self.route, self.confidence = route, confidence
        self.calls: list[str] = []

    async def decide(self, text: str) -> RouteDecision:
        self.calls.append(text)
        return RouteDecision(self.route, self.confidence, RouteReason.model_choice, False)


class FallbackRouter:
    async def decide(self, text: str) -> RouteDecision:
        return fallback(RouteReason.low_confidence, 0.2)


class SpyMemory:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def for_query(self, query: str) -> str | None:
        self.queries.append(query)
        return '{"notes": [{"title": "PRIVATE-NOTE-TEXT"}]}'


class SpyStarter:
    def __init__(self) -> None:
        self.questions: list[str] = []

    async def start(self, question: str):  # pragma: no cover - must never run
        self.questions.append(question)
        raise AssertionError("the casual path must not start a research")


def make(
    provider: FakeProvider | None = None,
    *,
    router=None,
    casual: CasualService | None | bool = True,
    memory: SpyMemory | None = None,
    starter: SpyStarter | None = None,
    store: ConversationStore | None = None,
):
    provider = provider or FakeProvider()
    store = store or ConversationStore()
    if casual is True:
        casual = CasualService()
    service = ChatService(
        provider,
        store,
        router=router or ChoiceRouter(),
        casual=casual or None,
        memory_context=memory,  # type: ignore[arg-type]
        research_starter=starter,  # type: ignore[arg-type]
    )
    return service, provider, store


def stream(service: ChatService, message: str = HELLO, conversation_id=None) -> list:
    async def run():
        return [
            item
            async for item in service.stream_with_activity(message, conversation_id)
        ]

    return asyncio.run(run())


def payloads(items) -> list[dict]:
    return [i.to_payload() for i in items if isinstance(i, ActivityEvent)]


def reply_of(items) -> str:
    return "".join(i.text for i in items if isinstance(i, ChatDelta))


def stored(store: ConversationStore) -> list[tuple[str, str]]:
    return [
        (m.role, m.content) for c in store._conversations.values() for m in c.messages
    ]


CASUAL_EVENTS = [
    {"stage": "received"},
    {"stage": "routing"},
    {"stage": "route_selected", "route": "casual", "decided": "casual", "fallback": False},
    {"stage": "generating"},
    {"stage": "done"},
]


# --- when the casual path is used ----------------------------------------------------------


def test_a_confident_casual_decision_is_answered_by_the_casual_path() -> None:
    memory, starter = SpyMemory(), SpyStarter()
    service, provider, store = make(memory=memory, starter=starter)
    items = stream(service)
    assert payloads(items) == CASUAL_EVENTS
    assert reply_of(items) == "Hello."
    done = items[-1]
    assert isinstance(done, ChatDone) and (done.provider, done.model) == ("fake", "fake-model")
    assert stored(store) == [("user", HELLO), ("assistant", "Hello.")]
    # Nothing but the personality and the conversation was consulted.
    assert memory.queries == [] and starter.questions == []
    assert not any(p["stage"] in {"memory_lookup", "researching"} for p in payloads(items))


def test_the_request_has_the_personality_and_the_message_only() -> None:
    service, provider, _ = make(memory=SpyMemory())
    stream(service, "PLAIN-MESSAGE")
    (request,) = provider.requests
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.messages[0].content == SYSTEM_PROMPT + CASUAL_RULES
    assert request.messages[1].content == "PLAIN-MESSAGE"
    assert "PRIVATE-NOTE-TEXT" not in json.dumps([m.content for m in request.messages])


def test_history_is_cut_to_the_last_six_turns_of_this_conversation() -> None:
    service, provider, store = make()
    first = stream(service, "turn 0")
    conversation_id = first[-1].conversation_id
    for index in range(1, 9):
        stream(service, f"turn {index}", conversation_id)
    request = provider.requests[-1]
    contents = [m.content for m in request.messages[1:]]
    # Six earlier turns (user + assistant) and the new message; turns 0 and 1 are gone.
    assert len(contents) == 2 * CASUAL_HISTORY_TURNS + 1
    assert contents[0] == "turn 2" and contents[-1] == "turn 8"
    assert "turn 1" not in contents and "turn 0" not in contents
    assert [m.role for m in request.messages[1:3]] == ["user", "assistant"]


def test_another_conversation_is_never_visible() -> None:
    service, provider, _ = make()
    stream(service, "SECRET-OF-CONVERSATION-A")
    stream(service, "hello from B")
    assert "SECRET-OF-CONVERSATION-A" not in json.dumps(
        [m.content for m in provider.requests[-1].messages]
    )


def test_other_decisions_and_router_fallbacks_do_not_use_it() -> None:
    for router in (ChoiceRouter(Route.memory), ChoiceRouter(Route.research), FallbackRouter()):
        main = MainProvider()
        service, _, _ = make(main, router=router)
        items = stream(service)
        assert reply_of(items) == "Main answer."
        assert all(p.get("route") != "casual" for p in payloads(items))
        assert all("casual_skip" not in p for p in payloads(items))


def test_a_low_confidence_casual_decision_runs_on_the_main_agent() -> None:
    service, provider, _ = make(MainProvider(), router=ChoiceRouter(confidence=0.3))
    items = stream(service)
    assert reply_of(items) == "Main answer."
    assert payloads(items)[2] == {
        "stage": "route_selected", "route": "main", "decided": "casual", "fallback": False,
        "casual_skip": "low_confidence",
    }


def test_without_a_router_the_casual_path_is_never_used() -> None:
    provider = MainProvider()
    service = ChatService(provider, ConversationStore(), casual=CasualService())
    items = asyncio.run(_collect(service))
    assert reply_of(items) == "Main answer."


async def _collect(service: ChatService) -> list:
    return [i async for i in service.stream_with_activity(HELLO)]


# --- the switch is off: exactly today's behavior ---------------------------------------------


def test_switch_off_is_the_old_behavior_with_memory_and_no_casual_fields() -> None:
    memory = SpyMemory()
    service, provider, _ = make(MainProvider(), casual=False, memory=memory)
    items = stream(service)
    assert payloads(items) == [
        {"stage": "received"},
        {"stage": "routing"},
        {"stage": "route_selected", "route": "main", "decided": "casual", "fallback": False},
        {"stage": "memory_lookup", "count": 0},
        {"stage": "generating"},
        {"stage": "done"},
    ]
    assert memory.queries == [HELLO]
    assert reply_of(items) == "Main answer."


# --- the daily cap -----------------------------------------------------------------------


def test_the_daily_cap_falls_back_to_the_main_agent_without_an_error() -> None:
    provider = FakeProvider()
    service, _, store = make(provider, casual=CasualService(daily_call_limit=2))
    for _ in range(2):
        assert reply_of(stream(service)) == "Hello."
    provider.deltas = ("Main ", "answer.")
    items = stream(service)
    assert reply_of(items) == "Main answer."
    assert payloads(items)[2] == {
        "stage": "route_selected", "route": "main", "decided": "casual", "fallback": False,
        "casual_skip": "over_budget",
    }
    assert payloads(items)[-1] == {"stage": "done"}
    assert len(stored(store)) == 6


def test_the_counter_starts_again_on_the_next_day() -> None:
    day = [date(2026, 10, 10)]
    casual = CasualService(daily_call_limit=1, today=lambda: day[0])
    assert casual.reserve() is True
    assert casual.reserve() is False
    day[0] += timedelta(days=1)
    assert casual.reserve() is True


def test_a_limit_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        CasualService(daily_call_limit=0)


# --- failures never reach the user as an error ----------------------------------------------


@pytest.mark.parametrize("mode", ["error", "empty", "boom"])
def test_a_failure_before_the_first_token_runs_the_main_agent(mode: str) -> None:
    casual_provider = FakeProvider()
    casual_provider.mode = mode
    casual_provider.deltas = ("Main ", "answer.")
    # One provider serves both paths in this app: make the second call work.
    calls = {"n": 0}
    original_stream = casual_provider.stream

    async def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            async for d in original_stream(request):
                yield d
        else:
            casual_provider.mode = "ok"
            async for d in original_stream(request):
                yield d

    casual_provider.stream = flaky  # type: ignore[method-assign]
    service, _, store = make(casual_provider)
    items = stream(service)
    events = payloads(items)
    assert reply_of(items) == "Main answer."
    assert [e["stage"] for e in events] == [
        "received", "routing", "route_selected", "generating", "done",
    ]
    assert events[2] == {
        "stage": "route_selected", "route": "main", "decided": "casual", "fallback": False,
        "casual_skip": "provider",
    }
    assert stored(store) == [("user", HELLO), ("assistant", "Main answer.")]
    assert "secret-detail" not in json.dumps(events)


def test_a_failure_after_the_first_token_is_an_error_and_nothing_is_saved() -> None:
    provider = FakeProvider()
    provider.mode = "late_error"
    service, _, store = make(provider)

    async def run():
        seen: list = []
        with pytest.raises(ProviderError):
            async for item in service.stream_with_activity(HELLO):
                seen.append(item)
        return seen

    items = asyncio.run(run())
    assert payloads(items)[-1] == {"stage": "error", "code": "provider"}
    assert stored(store) == []


def test_complete_uses_the_casual_path_and_falls_back() -> None:
    provider = FakeProvider()
    service, _, store = make(provider)
    events: list[ActivityEvent] = []
    result = asyncio.run(service.complete(HELLO, on_activity=events.append))
    assert result.reply == "Hello." and payloads(events) == CASUAL_EVENTS

    provider.mode = "error"
    events.clear()

    # The casual call fails; the Main Agent call (same provider) then fails too, which is the
    # ordinary provider error: nothing is invented.
    with pytest.raises(ProviderError):
        asyncio.run(service.complete(HELLO, on_activity=events.append))
    assert payloads(events)[2]["casual_skip"] == "provider"
    assert payloads(events)[-1] == {"stage": "error", "code": "provider"}
    assert len(stored(store)) == 2


def test_complete_fallback_answers_from_the_main_agent() -> None:
    provider = FakeProvider()
    calls = {"n": 0}
    original = provider.complete

    async def second_time_works(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("down")
        return await original(request)

    provider.complete = second_time_works  # type: ignore[method-assign]
    service, _, _ = make(provider)
    events: list[ActivityEvent] = []
    result = asyncio.run(service.complete(HELLO, on_activity=events.append))
    assert result.reply == "Hello."
    assert payloads(events)[2]["casual_skip"] == "provider"
    assert payloads(events)[-1] == {"stage": "done"}


# --- output limit ----------------------------------------------------------------------


def test_the_stream_is_cut_at_the_output_limit_and_the_model_call_is_closed() -> None:
    closed = []

    class Long:
        name, model = "long", "long-model"

        async def stream(self, request):
            try:
                for _ in range(100):
                    yield "x" * 50
            finally:
                closed.append(True)

    service = ChatService(
        Long(),  # type: ignore[arg-type]
        ConversationStore(),
        router=ChoiceRouter(),
        casual=CasualService(max_output_chars=120),
    )
    items = stream(service)
    assert reply_of(items) == "x" * 120
    assert payloads(items)[-1] == {"stage": "done"}
    assert closed == [True]
    assert CASUAL_MAX_OUTPUT_CHARS == 600


def test_complete_reply_is_cut_at_the_limit() -> None:
    provider = FakeProvider()
    provider.deltas = ("y" * 1000,)
    service, _, _ = make(provider, casual=CasualService(max_output_chars=300))
    assert asyncio.run(service.complete(HELLO)).reply == "y" * 300


# --- transcripts and activity vocabulary ---------------------------------------------------


def test_the_casual_turn_is_an_ordinary_message_pair_in_the_next_main_history() -> None:
    provider = FakeProvider()
    service, _, _ = make(provider)
    first = stream(service)
    service.router = ChoiceRouter(Route.memory)
    stream(service, "and now?", first[-1].conversation_id)
    contents = [m.content for m in provider.requests[-1].messages]
    assert HELLO in contents and "Hello." in contents


def test_activity_rules_for_the_casual_route() -> None:
    event = ActivityEvent.route_selected(ActivityRoute.CASUAL, ActivityRoute.CASUAL, False)
    assert event.to_payload() == CASUAL_EVENTS[2]
    skipped = ActivityEvent.route_selected(
        ActivityRoute.MAIN, ActivityRoute.CASUAL, False, casual_skip=CasualSkip.OVER_BUDGET
    )
    assert skipped.to_payload()["casual_skip"] == "over_budget"
    for bad in (
        lambda: ActivityEvent.route_selected(ActivityRoute.CASUAL, ActivityRoute.MEMORY, False),
        lambda: ActivityEvent.route_selected(ActivityRoute.CASUAL, ActivityRoute.CASUAL, True),
        lambda: ActivityEvent.route_selected(
            ActivityRoute.CASUAL, ActivityRoute.CASUAL, False, casual_skip=CasualSkip.PROVIDER
        ),
        lambda: ActivityEvent.route_selected(
            ActivityRoute.MAIN, ActivityRoute.MEMORY, False, casual_skip=CasualSkip.PROVIDER
        ),
        lambda: ActivityEvent(ActivityStage.GENERATING, casual_skip=CasualSkip.PROVIDER),
    ):
        with pytest.raises(ValueError):
            bad()


def test_hostile_text_stays_out_of_events() -> None:
    hostile = "</script> sk-live-0123456789 /etc/passwd"
    service, _, _ = make()
    assert payloads(stream(service, hostile)) == CASUAL_EVENTS


# --- configuration, app wiring and doctor ----------------------------------------------------


def settings(tmp_path: Path, **kwargs) -> Settings:
    return Settings(db_path=tmp_path / "x.sqlite3", **kwargs)


def test_settings_defaults_and_validation(tmp_path: Path) -> None:
    default = settings(tmp_path)
    assert default.casual is False and default.casual_daily_call_limit == 200
    for limit in (1, 10000):
        assert settings(tmp_path, casual_daily_call_limit=limit).casual_daily_call_limit == limit
    for bad in (0, -1, 10001, True, 1.5):
        with pytest.raises(ConfigError):
            settings(tmp_path, casual_daily_call_limit=bad)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        settings(tmp_path, casual="yes")  # type: ignore[arg-type]


def test_settings_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "x.sqlite3"))
    monkeypatch.setenv("JARVIS_CASUAL", "1")
    monkeypatch.setenv("JARVIS_CASUAL_DAILY_CALL_LIMIT", "50")
    loaded = Settings.from_env()
    assert loaded.casual is True and loaded.casual_daily_call_limit == 50
    monkeypatch.setenv("JARVIS_CASUAL", "maybe")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("JARVIS_CASUAL", "0")
    monkeypatch.setenv("JARVIS_CASUAL_DAILY_CALL_LIMIT", "0")
    with pytest.raises(ConfigError):
        Settings.from_env()


def sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def test_the_app_wires_the_casual_path_only_with_switch_and_router(tmp_path: Path) -> None:
    for kwargs, router, used in (
        ({"casual": True}, ChoiceRouter(), True),
        ({"casual": False}, ChoiceRouter(), False),
        ({"casual": True}, None, False),
    ):
        provider = MainProvider()
        app = create_app(settings(tmp_path, **kwargs), provider, router=router)
        with TestClient(app) as client:
            response = client.post("/api/chat/stream", json={"message": HELLO}, headers=ACTIVITY)
        events = sse(response.text)
        routes = [d for n, d in events if n == "activity" and d["stage"] == "route_selected"]
        text = "".join(d["text"] for n, d in events if n == "delta")
        if used:
            assert routes[0]["route"] == "casual" and text == "Main answer."
            assert len(provider.requests) == 1
            assert provider.requests[0].messages[0].content.endswith(CASUAL_RULES)
        else:
            assert all(r["route"] == "main" for r in routes)
            assert not provider.requests[0].messages[0].content.endswith(CASUAL_RULES)


def test_the_app_uses_the_requested_model_for_the_casual_answer(tmp_path: Path) -> None:
    default, other = FakeProvider("gemini", "default-model"), FakeProvider("groq", "alt-model")
    other.deltas = ("Alt.",)
    from backend.providers.choices import ModelRegistry

    models = ModelRegistry(
        ("groq:alt-model",), default, builder=lambda *_: other, ready=lambda _p: True
    )
    app = create_app(
        settings(tmp_path, casual=True, model_choices=("groq:alt-model",)),
        default,
        router=ChoiceRouter(),
        models=models,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/stream",
            json={"message": HELLO, "model_choice": "groq:alt-model"},
            headers=ACTIVITY,
        )
    done = sse(response.text)[-1][1]
    assert (done["provider"], done["model"]) == ("groq", "alt-model")
    assert default.requests == [] and len(other.requests) == 1


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    import os

    for name in list(os.environ):
        if name.startswith("JARVIS_") or name in {"GEMINI_API_KEY", "GROQ_API_KEY"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "x.sqlite3"))


def casual_check():
    return {c.area: c for c in doctor.run_checks()}["casual"]


def test_doctor_casual_row(clean_env, monkeypatch, capsys) -> None:
    assert (casual_check().status, casual_check().code) == ("OFF", "NOT_CONFIGURED")
    monkeypatch.setenv("JARVIS_CASUAL", "1")
    assert (casual_check().status, casual_check().code) == ("INCOMPLETE", "CASUAL_NEEDS_ROUTER")
    monkeypatch.setenv("JARVIS_ROUTER", "rule")
    assert (casual_check().status, casual_check().code) == (
        "INCOMPLETE", "CASUAL_NEEDS_CHAT_PROVIDER",
    )
    monkeypatch.setenv("JARVIS_LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-value")
    monkeypatch.setenv("JARVIS_GROQ_MODEL", "vendor/model-x")
    assert (casual_check().status, casual_check().code) == ("OK", "READY")
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "fake-key-value" not in out and "vendor/model-x" not in out
    assert "casual=on" in out
    monkeypatch.setenv("JARVIS_ROUTER", "off")
    assert casual_check().code == "CASUAL_NEEDS_ROUTER"
