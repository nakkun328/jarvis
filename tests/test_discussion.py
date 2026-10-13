"""The discussion engine and persona definitions, with fake providers only (no network)."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from backend import doctor
from backend.core.config import ConfigError, Settings
from backend.discussion import (
    DiscussionEngine,
    DiscussionError,
    DiscussionMessage,
    MessageKind,
    Persona,
    StopCode,
    load_personas,
    parse_personas,
    preset_personas,
)
from backend.discussion.engine import (
    MAX_CONSECUTIVE_FAILURES,
    MODERATOR_NAME,
    SUMMARY_MAX_CHARS,
    TURN_MAX_CHARS,
)
from backend.providers.base import CompletionRequest, CompletionResponse, ProviderError

pytestmark = pytest.mark.usefixtures("no_network")

MODELS = ("openai:model-a", "gemini:model-b", "groq:model-c")
TOPIC = "Should we move the club's records to a shared spreadsheet?"


def persona(name: str, model: str = MODELS[0], **kw) -> Persona:
    base = {
        "role": f"role-of-{name}",
        "stance": f"stance-of-{name}",
        "checklist": (f"check-of-{name}",),
        "color": "blue",
    }
    return Persona(name=name, model=model, **{**base, **kw})


def trio() -> list[Persona]:
    return [persona("Ann", MODELS[0]), persona("Bob", MODELS[1]), persona("Cy", MODELS[2])]


class Fake:
    """A scripted provider: ``replies`` are consumed in order; an Exception item is raised."""

    def __init__(self, replies=None, default="a new point"):
        self.replies = list(replies or [])
        self.default = default
        self.requests: list[CompletionRequest] = []

    async def complete(self, request):
        self.requests.append(request)
        item = self.replies.pop(0) if self.replies else self.default
        if isinstance(item, Exception):
            raise item
        return CompletionResponse(text=item, provider="fake", model="m")

    def stream(self, request):  # pragma: no cover - the engine never streams
        raise AssertionError("not used")


class Harness:
    def __init__(self, personas=None, providers=None, moderator=None):
        self.personas = personas or trio()
        self.providers = providers or {m: Fake() for m in MODELS}
        self.moderator = moderator or Fake(default="summary text")
        self.messages: list[DiscussionMessage] = []
        self.order: list[str] = []

    async def emit(self, message):
        self.messages.append(message)

    def resolve(self, model):
        self.order.append(model)
        return self.providers[model]

    def engine(self, topic=TOPIC, **extra):
        return DiscussionEngine(
            self.personas,
            topic,
            resolve_provider=self.resolve,
            moderator=self.moderator,
            emit=self.emit,
            **extra,
        )

    def run(self, **extra):
        return asyncio.run(self.engine(**extra).run())


# ---- personas ------------------------------------------------------------------------------


def test_valid_persona_and_allowlist_check():
    p = persona("Ann")
    assert p.as_dict()["model"] == MODELS[0]
    p.require_allowed(MODELS)
    with pytest.raises(DiscussionError) as err:
        p.require_allowed(("openai:other",))
    assert str(err.value) == "PERSONA_MODEL_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"name": ""}, "PERSONA_NAME_INVALID"),
        ({"name": "x" * 21}, "PERSONA_NAME_INVALID"),
        ({"name": " Ann"}, "PERSONA_NAME_INVALID"),
        ({"name": "A【n"}, "PERSONA_NAME_INVALID"),
        ({"name": "A\nn"}, "PERSONA_NAME_INVALID"),
        ({"name": 5}, "PERSONA_NAME_INVALID"),
        ({"role": ""}, "PERSONA_ROLE_INVALID"),
        ({"role": "r" * 101}, "PERSONA_ROLE_INVALID"),
        ({"stance": "s" * 501}, "PERSONA_STANCE_INVALID"),
        ({"stance": "bad\x00"}, "PERSONA_STANCE_INVALID"),
        ({"checklist": tuple("c" for _ in range(6))}, "PERSONA_CHECKLIST_INVALID"),
        ({"checklist": ("c" * 101,)}, "PERSONA_CHECKLIST_INVALID"),
        ({"checklist": ("",)}, "PERSONA_CHECKLIST_INVALID"),
        ({"checklist": ["list-not-tuple"]}, "PERSONA_CHECKLIST_INVALID"),
        ({"model": "gpt-free-text"}, "PERSONA_MODEL_INVALID"),
        ({"model": "other:model"}, "PERSONA_MODEL_INVALID"),
        ({"model": "openai:a,gemini:b"}, "PERSONA_MODEL_INVALID"),
        ({"color": "#ff0000"}, "PERSONA_COLOR_INVALID"),
    ],
)
def test_persona_validation_codes_and_no_user_text(kw, code):
    base = {
        "name": "Ann",
        "role": "r",
        "stance": "s",
        "checklist": ("c",),
        "model": MODELS[0],
        "color": "blue",
    }
    with pytest.raises(DiscussionError) as err:
        Persona(**{**base, **kw})
    assert str(err.value) == code
    for value in kw.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in str(err.value)


def test_persona_limits_accept_boundaries():
    Persona("n" * 20, "r" * 100, "s" * 500, tuple("c" * 100 for _ in range(5)), MODELS[0], "teal")


def test_presets_use_allowlist_models_in_turn():
    presets = preset_personas(MODELS[:2])
    assert [p.name for p in presets] == ["設計者", "批判役", "現実担当"]
    assert [p.model for p in presets] == [MODELS[0], MODELS[1], MODELS[0]]
    assert len({p.color for p in presets}) == 3
    assert "反対の根拠を必ず1つ挙げる" in presets[1].checklist
    with pytest.raises(DiscussionError):
        preset_personas(())


def raw(name, model=MODELS[0], **kw):
    return {"name": name, "role": "r", "model": model, **kw}


def test_loader_reads_file_and_assigns_colors(tmp_path: Path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"personas": [raw("A"), raw("B", MODELS[1], checklist=["x"])]}))
    loaded = load_personas(path, MODELS)
    assert [p.name for p in loaded] == ["A", "B"]
    assert loaded[1].checklist == ("x",)
    assert loaded[0].color != loaded[1].color


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("not json", "PERSONA_FILE_INVALID"),
        (b"\xff\xfe", "PERSONA_FILE_INVALID"),
        ("{}", "PERSONA_FILE_INVALID"),
        (json.dumps([raw("A")]), "DISCUSSION_PARTICIPANTS_INVALID"),
        (json.dumps([raw(str(i)) for i in range(5)]), "DISCUSSION_PARTICIPANTS_INVALID"),
        (json.dumps([raw("A"), raw("a")]), "PERSONA_DUPLICATE_NAME"),
        (json.dumps([raw("A"), raw("B", "openai:not-allowed")]), "PERSONA_MODEL_NOT_ALLOWED"),
        (json.dumps([raw("A"), raw("B", api_key="x")]), "PERSONA_FIELD_UNKNOWN"),
        (json.dumps([raw("A"), "text"]), "PERSONA_INVALID"),
        (json.dumps([raw("A"), raw("B", checklist="x")]), "PERSONA_CHECKLIST_INVALID"),
        ("x" * 70_000, "PERSONA_FILE_INVALID"),
    ],
)
def test_loader_rejects_with_fixed_codes(tmp_path: Path, content, code):
    path = tmp_path / "p.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(DiscussionError) as err:
        load_personas(path, MODELS)
    assert str(err.value) == code


def test_loader_missing_file_does_not_echo_path(tmp_path: Path):
    path = tmp_path / "secret-dir" / "p.json"
    with pytest.raises(DiscussionError) as err:
        load_personas(path, MODELS)
    assert str(err.value) == "PERSONA_FILE_UNREADABLE"
    assert "secret-dir" not in str(err.value)


def test_parse_accepts_plain_list():
    assert len(parse_personas([raw("A"), raw("B")], MODELS)) == 2


# ---- engine ----------------------------------------------------------------------------------


def test_round_robin_order_and_summary():
    h = Harness()
    result = h.run(rounds=2)
    turns = [m for m in h.messages if m.kind is MessageKind.TURN]
    assert [m.speaker for m in turns] == ["Ann", "Bob", "Cy"] * 2
    assert h.order == list(MODELS) * 2
    last = h.messages[-1]
    assert (last.kind, last.speaker, last.text) == (
        MessageKind.SUMMARY,
        MODERATOR_NAME,
        "summary text",
    )
    assert result.stop is StopCode.COMPLETED and result.summarized
    assert result.calls == 3 * 2 + 1 and result.turns == 6


def test_default_rounds_is_two():
    assert Harness().run().calls == 3 * 2 + 1


def test_prompt_has_topic_transcript_and_instruction():
    h = Harness()
    h.run(rounds=1)
    second = h.providers[MODELS[1]].requests[0]
    system, user = (m.content for m in second.messages)
    assert TOPIC in user
    assert "【Ann】a new point" in user  # the earlier turn
    assert "繰り返さず" in system and "繰り返さず" in user
    assert "role-of-Bob" in system and "stance-of-Bob" in system and "check-of-Bob" in system


def test_never_leaks_other_personas_settings():
    h = Harness()
    h.run(rounds=2)
    for model, mine in zip(MODELS, ("Ann", "Bob", "Cy"), strict=True):
        others = [n for n in ("Ann", "Bob", "Cy") if n != mine]
        for request in h.providers[model].requests:
            text = "\n".join(m.content for m in request.messages)
            assert f"role-of-{mine}" in text
            for other in others:
                for marker in ("role-of-", "stance-of-", "check-of-"):
                    assert f"{marker}{other}" not in text
    summary_prompt = "\n".join(m.content for m in h.moderator.requests[0].messages)
    assert "role-of-" not in summary_prompt and "stance-of-" not in summary_prompt


def test_turn_and_summary_are_capped():
    h = Harness(
        providers={m: Fake(default="x" * 5000) for m in MODELS},
        moderator=Fake(default="y" * 5000),
    )
    h.run(rounds=1)
    assert all(len(m.text) <= TURN_MAX_CHARS for m in h.messages if m.kind is MessageKind.TURN)
    assert len(h.messages[-1].text) == SUMMARY_MAX_CHARS


def test_transcript_is_bounded_and_older_turns_collapse():
    h = Harness(providers={m: Fake(default="z" * 600) for m in MODELS})
    h.run(rounds=3, transcript_chars=1500)
    last_prompt = h.providers[MODELS[2]].requests[-1].messages[1].content
    assert "以前の発言" in last_prompt and "省略" in last_prompt
    transcript = last_prompt.split("これまでの発言:\n")[1].split("\n\nあなたの番です")[0]
    assert len(transcript) <= 1500
    assert last_prompt.count("【") <= 3  # only the newest turns are kept verbatim


def test_rounds_and_participant_validation():
    h = Harness()
    for rounds in (0, 4, True, "2"):
        with pytest.raises(DiscussionError):
            h.engine(rounds=rounds)
    with pytest.raises(DiscussionError):
        h.engine(rounds=3, max_rounds=2)
    for topic in ("", "   ", "t" * 2001, None):
        with pytest.raises(DiscussionError):
            h.engine(topic=topic)
    for count in (1, 5):
        bad = Harness(personas=[persona(f"P{i}") for i in range(count)])
        with pytest.raises(DiscussionError):
            bad.engine()
    dup = Harness(personas=[persona("Ann"), persona("ann")])
    with pytest.raises(DiscussionError):
        dup.engine()
    with pytest.raises(DiscussionError):
        h.engine(transcript_chars=10)


def test_call_cap_matches_formula_and_is_enforced():
    four = Harness(personas=[*trio(), persona("Dee")])
    assert four.engine(rounds=3).call_cap == 4 * 3 + 1
    # With the counter already at the turn budget, the engine refuses the next turn call.
    engine = Harness().engine(rounds=1)
    engine._calls = engine.call_cap - 1
    result = asyncio.run(engine.run())
    assert result.stop is StopCode.CALL_CAP and not result.summarized


def test_cancellation_stops_before_next_call():
    h = Harness()
    seen = {"n": 0}

    def cancel():
        seen["n"] += 1
        return seen["n"] > 2  # allows two turns

    result = h.run(is_cancelled=cancel)
    assert result.stop is StopCode.CANCELLED and result.calls == 2
    assert not h.moderator.requests
    assert h.messages[-1].kind is MessageKind.NOTICE and h.messages[-1].code == "CANCELLED"


def test_async_cancellation_check_is_supported():
    h = Harness()

    async def cancel():
        return True

    result = h.run(is_cancelled=cancel)
    assert result.stop is StopCode.CANCELLED and result.calls == 0


def test_single_persona_failure_is_skipped_and_recorded():
    providers = {
        MODELS[0]: Fake(),
        MODELS[1]: Fake([ProviderError("boom"), "recovered"]),
        MODELS[2]: Fake(),
    }
    h = Harness(providers=providers)
    result = h.run(rounds=2)
    assert result.stop is StopCode.COMPLETED and result.summarized
    assert result.skipped == (("Bob", "PROVIDER_ERROR"),)
    assert result.turns == 5 and result.calls == 7
    notices = [m for m in h.messages if m.kind is MessageKind.NOTICE]
    assert [n.code for n in notices] == ["PERSONA_SKIPPED"]


def test_empty_reply_and_unresolvable_model_are_failures():
    providers = {MODELS[0]: Fake([""]), MODELS[1]: Fake(), MODELS[2]: Fake()}
    h = Harness(providers=providers)

    def resolve(model):
        if model == MODELS[2]:
            raise KeyError("do-not-leak")
        return providers[model]

    engine = DiscussionEngine(
        h.personas, TOPIC, resolve_provider=resolve, moderator=h.moderator, emit=h.emit, rounds=1
    )
    result = asyncio.run(engine.run())
    assert [code for _, code in result.skipped] == ["EMPTY_REPLY", "MODEL_UNAVAILABLE"]
    assert result.stop is StopCode.COMPLETED and result.turns == 1


def test_three_consecutive_failures_stop_without_summary():
    providers = {m: Fake([ProviderError("down")] * 5) for m in MODELS}
    h = Harness(providers=providers)
    result = h.run(rounds=3)
    assert MAX_CONSECUTIVE_FAILURES == 3
    assert result.stop is StopCode.PROVIDER_FAILURES
    assert result.calls == 3 and result.turns == 0 and not result.summarized
    assert not h.moderator.requests
    assert h.messages[-1].code == "PROVIDER_FAILURES"


def test_failures_must_be_consecutive():
    err = ProviderError("x")
    providers = {
        MODELS[0]: Fake([err, "ok", err, "ok"]),
        MODELS[1]: Fake([err, "ok", err, "ok"]),
        MODELS[2]: Fake(),
    }
    result = Harness(providers=providers).run(rounds=2)
    assert result.stop is StopCode.COMPLETED and len(result.skipped) == 2


def test_moderator_failure_gives_notice():
    h = Harness(moderator=Fake([ProviderError("down")]))
    result = h.run(rounds=1)
    assert result.stop is StopCode.COMPLETED and not result.summarized
    assert h.messages[-1].code == "SUMMARY_FAILED"


def test_call_timeout_is_a_failure():
    class Slow(Fake):
        async def complete(self, request):
            await asyncio.sleep(5)

    providers = {MODELS[0]: Slow(), MODELS[1]: Fake(), MODELS[2]: Fake()}
    result = Harness(providers=providers).run(rounds=1, call_timeout_seconds=0.01)
    assert result.skipped == (("Ann", "TIMEOUT"),)


def test_task_cancellation_propagates():
    class Hang(Fake):
        async def complete(self, request):
            await asyncio.sleep(30)

    h = Harness(providers={m: Hang() for m in MODELS})

    async def go():
        task = asyncio.create_task(h.engine().run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_logs_never_contain_prompt_turn_or_secret_text(caplog):
    marker = "PRIVATE-TOPIC-TEXT"
    leak = "KEY-LOOKING-VALUE-123"
    providers = {
        MODELS[0]: Fake([ProviderError(leak), f"turn {marker}"]),
        MODELS[1]: Fake(default=f"reply {marker}"),
        MODELS[2]: Fake(default=f"reply {marker}"),
    }
    h = Harness(providers=providers, moderator=Fake(default=f"sum {marker}"))
    with caplog.at_level(logging.DEBUG):
        h.run(topic=f"{TOPIC} {marker}", rounds=1)
    logged = "\n".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
    assert caplog.records
    for forbidden in (marker, leak, "role-of-", "stance-of-", "check-of-", "Ann", "Bob"):
        assert forbidden not in logged


# ---- settings and doctor ---------------------------------------------------------------------


def base_settings(**kw):
    return Settings(db_path=Path("x.sqlite3"), **kw)


def test_settings_defaults_and_validation(monkeypatch):
    s = base_settings()
    assert (s.discussion, s.discussion_max_rounds, s.discussion_per_day_limit) == (False, 3, 10)
    for kw in (
        {"discussion": "yes"},
        {"discussion_max_rounds": 0},
        {"discussion_max_rounds": 4},
        {"discussion_max_rounds": True},
        {"discussion_per_day_limit": 0},
        {"discussion_per_day_limit": 101},
    ):
        with pytest.raises(ConfigError):
            base_settings(**kw)
    monkeypatch.setenv("JARVIS_DISCUSSION", "on")
    monkeypatch.setenv("JARVIS_DISCUSSION_MAX_ROUNDS", "2")
    monkeypatch.setenv("JARVIS_DISCUSSION_PER_DAY_LIMIT", "5")
    s = Settings.from_env()
    assert (s.discussion, s.discussion_max_rounds, s.discussion_per_day_limit) == (True, 2, 5)
    monkeypatch.setenv("JARVIS_DISCUSSION_MAX_ROUNDS", "9")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_doctor_row_off_and_incomplete(monkeypatch):
    for name in (
        "JARVIS_DISCUSSION",
        "JARVIS_MODEL_CHOICES",
        "JARVIS_LLM_PROVIDER",
        "JARVIS_DISCUSSION_MAX_ROUNDS",
        "JARVIS_DISCUSSION_PER_DAY_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    rows = {c.area: c for c in doctor.run_checks()}
    assert (rows["discussion"].status, rows["discussion"].code) == ("OFF", "NOT_CONFIGURED")
    monkeypatch.setenv("JARVIS_DISCUSSION", "1")
    rows = {c.area: c for c in doctor.run_checks()}
    assert rows["discussion"].status == "INCOMPLETE"
    assert rows["discussion"].code == "CHAT_PROVIDER_MISSING"
    assert rows["discussion"].detail == "discussion=on, rounds<=3, per_day<=10"
