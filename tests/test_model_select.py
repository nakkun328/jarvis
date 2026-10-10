"""Owner-selectable chat models: allowlist parsing, strict validation, lazy per-entry providers."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend.api.app import create_app
from backend.core.config import MAX_MODEL_CHOICES, ConfigError, Settings, parse_model_choices
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.providers.choices import ModelRegistry, ModelUnavailable, UnknownModelChoice

LEAK_PROBE = "sekret-key-do-not-leak-123"


class Fake:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model
        self.requests = 0
        self.closed = False

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests += 1
        return CompletionResponse(f"reply from {self.name}:{self.model}", self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        self.requests += 1
        yield f"stream from {self.name}:{self.model}"

    async def aclose(self) -> None:
        self.closed = True


CHOICES = ("openai:gpt-a", "gemini:gem-b", "gemini:gem-c")


class Harness:
    def __init__(self, ready: set[str]) -> None:
        self.built: list[str] = []
        self.made: dict[str, Fake] = {}
        self.default = Fake("openai", "gpt-a")
        self.registry = ModelRegistry(
            CHOICES,
            self.default,
            builder=self._build,
            ready=lambda provider: provider in ready,
        )

    def _build(self, provider: str, model: str) -> Fake:
        entry = f"{provider}:{model}"
        self.built.append(entry)
        self.made[entry] = Fake(provider, model)
        return self.made[entry]


def client_for(tmp_path: Path, harness: Harness) -> TestClient:
    settings = Settings(db_path=tmp_path / "m.sqlite3", model_choices=CHOICES)
    return TestClient(create_app(settings, harness.default, models=harness.registry))


def test_parse_accepts_valid_and_blank() -> None:
    assert parse_model_choices("") == ()
    assert parse_model_choices("  ") == ()
    assert parse_model_choices("openai:gpt-6-luna, gemini:gemini-2.5-flash") == (
        "openai:gpt-6-luna",
        "gemini:gemini-2.5-flash",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "gpt-4",  # no provider
        "anthropic:claude",  # unknown provider
        "openai:",  # empty model
        "openai:a b",  # space inside
        "openai:../x",
        "openai:a,",  # empty entry
        "OPENAI:gpt",  # strict lowercase provider
        "openai:a,openai:a",  # duplicate
        ",".join(f"openai:m{i}" for i in range(MAX_MODEL_CHOICES + 1)),
        "openai:" + "a" * 65,
    ],
)
def test_parse_rejects_malformed(raw: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_model_choices(raw)
    assert raw.strip() == "" or raw not in str(info.value)


def test_settings_validates_and_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "x", model_choices=("nope",))
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "x", model_choices=("openai:a", "openai:a"))
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "x"))
    monkeypatch.setenv("JARVIS_MODEL_CHOICES", "openai:a,gemini:b")
    assert Settings.from_env().model_choices == ("openai:a", "gemini:b")
    monkeypatch.setenv("JARVIS_MODEL_CHOICES", "bad")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.delenv("JARVIS_MODEL_CHOICES")
    assert Settings.from_env().model_choices == ()


def test_registry_validates_strictly_and_builds_lazily_once() -> None:
    h = Harness(ready={"gemini"})
    assert h.registry.provider_for(None) is h.default
    assert h.registry.provider_for("openai:gpt-a") is h.default  # the default needs no build
    for bad in ("", "gemini:other", "GEMINI:gem-b", "gemini:gem-b ", "openai:gpt-a,gemini:gem-b"):
        with pytest.raises(UnknownModelChoice):
            h.registry.provider_for(bad)
    assert h.built == []
    first = h.registry.provider_for("gemini:gem-b")
    assert h.registry.provider_for("gemini:gem-b") is first
    assert h.built == ["gemini:gem-b"]


def test_registry_unavailable_provider_is_refused_without_building() -> None:
    h = Harness(ready=set())
    with pytest.raises(ModelUnavailable):
        h.registry.provider_for("gemini:gem-b")
    assert h.built == []


def test_models_endpoint_reports_availability_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", LEAK_PROBE)
    h = Harness(ready={"gemini"})
    with client_for(tmp_path, h) as client:
        response = client.get("/api/models")
    assert response.status_code == 200
    assert response.json() == [
        {"id": "openai:gpt-a", "provider": "openai", "model": "gpt-a",
         "available": True, "is_default": True},
        {"id": "gemini:gem-b", "provider": "gemini", "model": "gem-b",
         "available": True, "is_default": False},
        {"id": "gemini:gem-c", "provider": "gemini", "model": "gem-c",
         "available": True, "is_default": False},
    ]
    assert LEAK_PROBE not in response.text
    h2 = Harness(ready=set())
    with client_for(tmp_path, h2) as client:
        flags = {m["id"]: m["available"] for m in client.get("/api/models").json()}
    assert flags == {"openai:gpt-a": True, "gemini:gem-b": False, "gemini:gem-c": False}


def test_models_endpoint_is_empty_without_choices(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "n.sqlite3"), Fake("fake", "m"))
    with TestClient(app) as client:
        assert client.get("/api/models").json() == []
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
        refused = client.post("/api/chat", json={"message": "hi", "model_choice": "openai:x"})
        assert refused.status_code == 400
        assert refused.json() == {"detail": "unknown model choice"}


def test_chat_uses_chosen_provider_and_default_otherwise(tmp_path: Path) -> None:
    h = Harness(ready={"gemini"})
    with client_for(tmp_path, h) as client:
        plain = client.post("/api/chat", json={"message": "hi"}).json()
        chosen = client.post(
            "/api/chat", json={"message": "hi", "model_choice": "gemini:gem-b"}
        ).json()
        again = client.post(
            "/api/chat", json={"message": "hi", "model_choice": "gemini:gem-b"}
        ).json()
    assert (plain["provider"], plain["model"]) == ("openai", "gpt-a")
    assert (chosen["provider"], chosen["model"]) == ("gemini", "gem-b")
    assert again["reply"] == chosen["reply"]
    assert h.default.requests == 1
    assert h.made["gemini:gem-b"].requests == 2
    assert h.built == ["gemini:gem-b"]
    assert h.made["gemini:gem-b"].closed  # closed with the app


def test_stream_uses_chosen_provider(tmp_path: Path) -> None:
    h = Harness(ready={"gemini"})
    with client_for(tmp_path, h) as client:
        response = client.post(
            "/api/chat/stream", json={"message": "hi", "model_choice": "gemini:gem-c"}
        )
    assert response.status_code == 200
    assert "stream from gemini:gem-c" in response.text
    assert '"provider": "gemini"' in response.text and '"model": "gem-c"' in response.text
    assert h.default.requests == 0


@pytest.mark.parametrize("route", ["/api/chat", "/api/chat/stream"])
def test_unknown_choice_is_a_fixed_400_and_reaches_no_provider(tmp_path: Path, route: str) -> None:
    h = Harness(ready={"gemini", "openai"})
    with client_for(tmp_path, h) as client:
        for bad in ("gemini:gemini-evil", "x", "", "openai:gpt-a\n"):
            response = client.post(route, json={"message": "hi", "model_choice": bad})
            assert response.status_code == 400
            assert response.json() == {"detail": "unknown model choice"}
            assert bad.strip() == "" or bad not in response.text
        too_long = client.post(route, json={"message": "hi", "model_choice": "a" * 101})
        assert too_long.status_code == 422
    assert h.built == [] and h.default.requests == 0


@pytest.mark.parametrize("route", ["/api/chat", "/api/chat/stream"])
def test_unavailable_choice_is_a_fixed_503(tmp_path: Path, route: str) -> None:
    h = Harness(ready=set())
    with client_for(tmp_path, h) as client:
        response = client.post(route, json={"message": "hi", "model_choice": "gemini:gem-b"})
    assert response.status_code == 503
    assert response.json() == {"detail": "model choice unavailable"}
    assert h.built == []


def test_only_the_allowlist_string_is_logged(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    h = Harness(ready={"gemini"})
    with client_for(tmp_path, h) as client:
        client.post("/api/chat", json={"message": "hi", "model_choice": "gemini:gem-b"})
        client.post("/api/chat", json={"message": "hi", "model_choice": "gemini:evil-name"})
    err = capfd.readouterr().err
    lines = [json.loads(x) for x in err.splitlines() if x.startswith("{")]
    chosen = [x["model_choice"] for x in lines if x["event"] == "chat.model_selected"]
    assert chosen == ["gemini:gem-b"]
    assert "evil-name" not in err


def test_models_endpoint_requires_login_when_auth_is_on(tmp_path: Path) -> None:
    from backend.auth.passwords import hash_passphrase

    settings = Settings(
        db_path=tmp_path / "a.sqlite3",
        auth_passphrase_hash=hash_passphrase("fake passphrase for tests", n=16, r=1, p=1),
        auth_signing_key="k" * 40,
        model_choices=CHOICES,
    )
    h = Harness(ready=set())
    app = create_app(settings, h.default, models=h.registry)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/models").status_code == 401


# --- doctor -------------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    import os

    for name in list(os.environ):
        keys = ("GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY")
        if name.startswith("JARVIS_") or name in keys:
            monkeypatch.delenv(name)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "d.sqlite3"))
    return monkeypatch


def models_check() -> doctor.Check:
    return {c.area: c for c in doctor.run_checks()}["models"]


def test_doctor_models_row(clean_env: pytest.MonkeyPatch) -> None:
    env = clean_env
    assert (models_check().status, models_check().code) == ("OFF", "NOT_CONFIGURED")
    env.setenv("JARVIS_MODEL_CHOICES", "gemini:gem-b,openai:gpt-a")
    assert models_check().code == "CHAT_PROVIDER_MISSING"
    env.setenv("JARVIS_LLM_PROVIDER", "gemini")
    env.setenv("GEMINI_API_KEY", LEAK_PROBE)
    env.setenv("JARVIS_GEMINI_MODEL", "gem-b")
    check = models_check()
    assert (check.status, check.code) == ("INCOMPLETE", "MODEL_CHOICE_UNAVAILABLE")  # no openai key
    env.setenv("OPENAI_API_KEY", LEAK_PROBE + "2")
    check = models_check()
    if doctor._installed("openai"):
        assert (check.status, check.code) == ("OK", "READY")
    rendered = json.dumps([c.as_dict() for c in doctor.run_checks()]) + doctor.render_text(
        doctor.run_checks(), "127.0.0.1"
    )
    assert LEAK_PROBE not in rendered
    env.setenv("JARVIS_MODEL_CHOICES", "bogus")
    assert doctor.run_checks()[0].code == "CONFIG_INVALID"


def test_selector_markup_and_script_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="model-select"' in html and "data-app-header" in html
    script = (root / "frontend" / "model-select.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "モデル" in script
