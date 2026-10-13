"""Custom personas: the JSON store and its API (docs/discussion.md). Fakes and tmp dirs only."""

import asyncio
import logging
import os
import stat
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.auth.limiter import LoginLimiter
from backend.auth.passwords import hash_passphrase
from backend.auth.service import AuthService
from backend.core.config import ConfigError, Settings
from backend.core.database import SCHEMA_VERSION
from backend.discussion import DiscussionError
from backend.discussion import store as store_module
from backend.discussion.personas import Persona
from backend.discussion.service import DiscussionService
from backend.discussion.store import MAX_STORE_BYTES, PersonaStore
from tests.test_discussion_api import MODELS, TOPIC, Env

pytestmark = pytest.mark.usefixtures("no_network")

HEADERS = {"X-Jarvis-Confirm": "1"}
SENTINEL = "PERSONA-TEXT-SENTINEL"


def body(name: str = "調査係", **over):
    data = {
        "name": name,
        "role": "資料を集める",
        "stance": f"{SENTINEL} 事実を重んじる",
        "checklist": ["出典", "日付"],
        "model": MODELS[0],
        "color": "teal",
    }
    data.update(over)
    return data


def persona(name: str = "調査係", **over) -> Persona:
    data = body(name, **over)
    return Persona(
        data["name"],
        data["role"],
        data["stance"],
        tuple(data["checklist"]),
        data["model"],
        data["color"],
    )


def code(caught: pytest.ExceptionInfo) -> str:
    return str(caught.value)


# ----- the store -----


def test_store_crud_file_mode_and_format(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    store = PersonaStore(path)
    assert store.list() == () and store.state() == "ok"
    store.create(persona("調査係"), MODELS)
    store.create(persona("記録係", color="red"), MODELS)
    assert [p.name for p in store.list()] == ["調査係", "記録係"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8").startswith('{"version":1,"personas":[')
    store.replace("調査係", persona("調査係2", role="別の役"), MODELS)
    assert store.get("調査係2").role == "別の役" and store.get("調査係") is None
    store.delete("記録係")
    assert [p.name for p in store.list()] == ["調査係2"]
    assert sorted(f.name for f in tmp_path.iterdir()) == ["p.json", "p.json.bak"]
    assert stat.S_IMODE((tmp_path / "p.json.bak").stat().st_mode) == 0o600


def test_store_validation_codes(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "p.json")
    store.create(persona("Alpha"), MODELS)
    cases = [
        (persona("alpha"), "PERSONA_DUPLICATE_NAME"),  # case-folded
        (persona("設計者"), "PERSONA_NAME_RESERVED"),
        (persona("批判役"), "PERSONA_NAME_RESERVED"),
        (persona("まとめ"), "PERSONA_NAME_RESERVED"),
        (persona("お知らせ"), "PERSONA_NAME_RESERVED"),
        (persona("まとめ役"), "PERSONA_NAME_RESERVED"),
        (persona("システム"), "PERSONA_NAME_RESERVED"),
        (persona("Beta", model="openai:other"), "PERSONA_MODEL_NOT_ALLOWED"),
    ]
    for item, expected in cases:
        with pytest.raises(DiscussionError) as caught:
            store.create(item, MODELS)
        assert code(caught) == expected
    with pytest.raises(DiscussionError) as caught:
        store.replace("nobody", persona("Gamma"), MODELS)
    assert code(caught) == "PERSONA_NOT_FOUND"
    with pytest.raises(DiscussionError) as caught:
        store.delete("nobody")
    assert code(caught) == "PERSONA_NOT_FOUND"
    store.create(persona("Beta"), MODELS)
    with pytest.raises(DiscussionError) as caught:
        store.replace("Beta", persona("ALPHA"), MODELS)  # rename onto another persona
    assert code(caught) == "PERSONA_DUPLICATE_NAME"
    store.replace("beta", persona("Beta"), MODELS)  # same name, any case in the path


def test_store_limit_of_twenty(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "p.json")
    for index in range(20):
        store.create(persona(f"P{index}"), MODELS)
    with pytest.raises(DiscussionError) as caught:
        store.create(persona("P20"), MODELS)
    assert code(caught) == "PERSONA_LIMIT"
    assert len(store.list()) == 20


def test_store_size_cap_leaves_the_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "MAX_STORE_BYTES", 6000)
    store = PersonaStore(tmp_path / "p.json")
    big = "あ" * 100
    for index in range(20):
        item = persona(f"P{index}", stance="い" * 500, checklist=[big] * 5)
        try:
            store.create(item, MODELS)
        except DiscussionError as error:
            assert str(error) == "PERSONA_STORE_TOO_LARGE"
            break
    else:
        pytest.fail("the size cap was never reached")
    assert 0 < (tmp_path / "p.json").stat().st_size <= 6000
    assert store.state() == "ok"


def test_no_path_or_missing_directory_is_unavailable_and_creates_nothing(tmp_path: Path) -> None:
    for store in (PersonaStore(None), PersonaStore(tmp_path / "missing" / "p.json")):
        assert store.state() == "unavailable"
        with pytest.raises(DiscussionError) as caught:
            store.create(persona(), MODELS)
        assert code(caught) == "PERSONA_STORE_UNAVAILABLE"
    assert not (tmp_path / "missing").exists()


def test_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text('{"version":1,"personas":[]}', encoding="utf-8")
    link = tmp_path / "p.json"
    link.symlink_to(target)
    store = PersonaStore(link)
    assert store.state() == "unavailable"
    with pytest.raises(DiscussionError) as caught:
        store.create(persona(), MODELS)
    assert code(caught) == "PERSONA_STORE_UNAVAILABLE"
    assert target.read_text(encoding="utf-8") == '{"version":1,"personas":[]}'
    # A symlinked .bak is refused too and nothing is written through it.
    clean = tmp_path / "q.json"
    other = PersonaStore(clean)
    other.create(persona(), MODELS)
    outside = tmp_path / "outside.txt"
    (tmp_path / "q.json.bak").symlink_to(outside)
    with pytest.raises(DiscussionError):
        other.create(persona("二人目"), MODELS)
    assert not outside.exists()


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'{"version":2,"personas":[]}',
        b'{"version":true,"personas":[]}',
        b'{"version":1,"personas":[],"extra":1}',
        b'{"version":1,"personas":[{"name":"x"}]}',
        '{"version":1,"personas":[{"name":"設計者","role":"r","model":"openai:model-a"}]}'.encode(),
        b'{"version":1,"personas":'
        b'[{"name":"a","role":"r","model":"openai:m"},{"name":"A","role":"r","model":"openai:m"}]}',
    ],
)
def test_corrupt_file_is_reported_and_never_overwritten(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "p.json"
    path.write_bytes(content)
    store = PersonaStore(path)
    assert store.state() == "corrupt"
    for action in (
        lambda: store.list(),
        lambda: store.create(persona(), MODELS),
        lambda: store.delete("a"),
    ):
        with pytest.raises(DiscussionError) as caught:
            action()
        assert code(caught) == "PERSONA_STORE_CORRUPT"
    assert path.read_bytes() == content
    assert not (tmp_path / "p.json.bak").exists()


def test_oversized_file_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_bytes(b" " * (MAX_STORE_BYTES + 1))
    assert PersonaStore(path).state() == "corrupt"


def test_bak_keeps_the_previous_good_file(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    store = PersonaStore(path)
    store.create(persona("A"), MODELS)
    first = path.read_bytes()
    store.create(persona("B"), MODELS)
    assert (tmp_path / "p.json.bak").read_bytes() == first
    second = path.read_bytes()
    store.delete("A")
    assert (tmp_path / "p.json.bak").read_bytes() == second


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_failure_mid_write_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path = tmp_path / "p.json"
    store = PersonaStore(path)
    store.create(persona("A"), MODELS)
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk exploded")

    monkeypatch.setattr(store_module.os, failing, boom)
    with pytest.raises(DiscussionError) as caught:
        store.create(persona("B"), MODELS)
    assert code(caught) == "PERSONA_STORE_UNAVAILABLE"
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert sorted(f.name for f in tmp_path.iterdir()) == ["p.json"]  # no temp file left
    assert [p.name for p in store.list()] == ["A"]


def test_failure_replacing_the_main_file_after_the_bak_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "p.json"
    store = PersonaStore(path)
    store.create(persona("A"), MODELS)
    before = path.read_bytes()
    real = os.replace
    calls = []

    def second_fails(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("no")
        return real(source, target)

    monkeypatch.setattr(store_module.os, "replace", second_fails)
    with pytest.raises(DiscussionError):
        store.create(persona("B"), MODELS)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in store.list()] == ["A"]


def test_concurrent_creates_do_not_lose_updates(tmp_path: Path) -> None:
    store = PersonaStore(tmp_path / "p.json")
    errors: list[str] = []

    def add(index: int) -> None:
        try:
            store.create(persona(f"P{index}"), MODELS)
        except DiscussionError as error:
            errors.append(str(error))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(store.list()) == 12


# ----- settings -----


def test_settings_path_validation_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "d.sqlite3", discussion_personas_path=Path(""))
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "d.sqlite3", discussion_personas_path=tmp_path / "d.sqlite3")
    for name in ("JARVIS_DB_PATH", "JARVIS_DISCUSSION_PERSONAS_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().discussion_personas_path is None
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "d.sqlite3"))
    assert Settings.from_env().discussion_personas_path == tmp_path / "discussion-personas.json"
    monkeypatch.setenv("JARVIS_DISCUSSION_PERSONAS_PATH", str(tmp_path / "x.json"))
    assert Settings.from_env().discussion_personas_path == tmp_path / "x.json"
    monkeypatch.setenv("JARVIS_DISCUSSION_PERSONAS_PATH", "  ")
    with pytest.raises(ConfigError) as caught:
        Settings.from_env()
    assert "  " not in str(caught.value).replace("must not be empty", "")


def test_doctor_row_shows_only_whether_editing_is_available(tmp_path: Path) -> None:
    from backend.doctor import OK, _discussion

    ok_chat = type("C", (), {"status": OK})()
    for path, word in (
        (tmp_path / "p.json", "available"),
        (None, "unavailable"),
        (tmp_path / "gone" / "p.json", "unavailable"),
    ):
        settings = Settings(
            db_path=tmp_path / "d.sqlite3",
            discussion=True,
            model_choices=MODELS,
            discussion_personas_path=path,
        )
        detail = _discussion(settings, ok_chat).detail
        assert f"persona_editing={word}" in detail
        assert str(tmp_path) not in detail


# ----- the API -----


def make_app(tmp_path: Path, **overrides):
    env = Env(tmp_path)
    settings = Settings(
        db_path=tmp_path / "db.sqlite3",
        discussion=True,
        model_choices=MODELS,
        discussion_personas_path=tmp_path / "personas.json",
        **overrides,
    )
    return env, settings, create_app(settings, env.default, models=env.registry)


def test_api_crud_and_listing(tmp_path: Path) -> None:
    env, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        listing = client.get("/api/discussions/personas")
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "no-store"
        data = listing.json()
        assert [p["name"] for p in data["presets"]] == ["設計者", "批判役", "現実担当"]
        assert (data["custom"], data["max_custom"], data["store"]) == ([], 20, "ok")

        created = client.post("/api/discussions/personas", json=body(), headers=HEADERS)
        assert created.status_code == 201
        assert created.json() == {**body(), "available": True}
        again = client.post("/api/discussions/personas", json=body("調査係"), headers=HEADERS)
        assert (again.status_code, again.json()) == (409, {"detail": "PERSONA_DUPLICATE_NAME"})

        # The config call carries the same objects.
        config = client.get("/api/discussions/config").json()
        assert config["custom_personas"] == client.get("/api/discussions/personas").json()["custom"]
        assert config["max_custom_personas"] == 20

        changed = client.put(
            f"/api/discussions/personas/{quote('調査係')}",
            json=body("調査係", role="新しい役"),
            headers=HEADERS,
        )
        assert (changed.status_code, changed.json()["role"]) == (200, "新しい役")
        missing = client.put("/api/discussions/personas/nobody", json=body("x"), headers=HEADERS)
        assert (missing.status_code, missing.json()) == (404, {"detail": "PERSONA_NOT_FOUND"})
        deleted = client.delete(f"/api/discussions/personas/{quote('調査係')}", headers=HEADERS)
        assert (deleted.status_code, deleted.json()) == (200, {"deleted": True})
        assert client.get("/api/discussions/personas").json()["custom"] == []
        gone = client.delete(f"/api/discussions/personas/{quote('調査係')}", headers=HEADERS)
        assert gone.status_code == 404


def test_api_validation_errors_never_echo_the_input(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        post = lambda payload: client.post(  # noqa: E731
            "/api/discussions/personas", json=payload, headers=HEADERS
        )
        cases = [
            (body(role=""), 400, "PERSONA_ROLE_INVALID"),
            (body(name=""), 400, "PERSONA_NAME_INVALID"),
            (body(name="【悪】"), 400, "PERSONA_NAME_INVALID"),
            (body(model="openai:not-listed"), 400, "PERSONA_MODEL_NOT_ALLOWED"),
            (body(model="bogus"), 400, "PERSONA_MODEL_INVALID"),
            (body(color="pink"), 400, "PERSONA_COLOR_INVALID"),
            (body(name="まとめ"), 400, "PERSONA_NAME_RESERVED"),
            (body(name="設計者"), 400, "PERSONA_NAME_RESERVED"),
            (body(checklist="no"), 400, "PERSONA_CHECKLIST_INVALID"),
            ({**body(), "api_key": SENTINEL}, 400, "DISCUSSION_REQUEST_INVALID"),
        ]
        for payload, status, expected in cases:
            response = post(payload)
            assert (response.status_code, response.json()) == (status, {"detail": expected})
            assert SENTINEL not in response.text
        for raw in (b"not json", b"[]", b"null"):
            response = client.post("/api/discussions/personas", content=raw, headers=HEADERS)
            assert response.json() == {"detail": "DISCUSSION_REQUEST_INVALID"}
        long_name = quote("あ" * 21)
        response = client.delete(f"/api/discussions/personas/{long_name}", headers=HEADERS)
        assert response.json() == {"detail": "PERSONA_NAME_INVALID"}


def test_api_body_size_cap(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        huge = b'{"name":"' + b"a" * (70 * 1024) + b'"}'
        response = client.post("/api/discussions/personas", content=huge, headers=HEADERS)
        assert (response.status_code, response.json()) == (
            400,
            {"detail": "DISCUSSION_REQUEST_INVALID"},
        )


def test_api_limit_and_store_unavailable(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        for index in range(20):
            ok = client.post("/api/discussions/personas", json=body(f"P{index}"), headers=HEADERS)
            assert ok.status_code == 201
        full = client.post("/api/discussions/personas", json=body("P20"), headers=HEADERS)
        assert (full.status_code, full.json()) == (409, {"detail": "PERSONA_LIMIT"})

    env = Env(tmp_path)
    settings = Settings(db_path=tmp_path / "db.sqlite3", discussion=True, model_choices=MODELS)
    with TestClient(create_app(settings, env.default, models=env.registry)) as client:
        listing = client.get("/api/discussions/personas").json()
        assert listing["store"] == "unavailable" and listing["custom"] == []
        assert len(listing["presets"]) == 3
        response = client.post("/api/discussions/personas", json=body(), headers=HEADERS)
        assert (response.status_code, response.json()) == (
            503,
            {"detail": "PERSONA_STORE_UNAVAILABLE"},
        )
        # Presets still start a discussion without a store.
        start = client.post(
            "/api/discussions",
            json={"topic": TOPIC, "personas": ["設計者", "批判役"], "rounds": 1},
            headers=HEADERS,
        )
        assert start.status_code == 201


def test_api_corrupt_store_is_reported_and_kept(tmp_path: Path) -> None:
    _, settings, app = make_app(tmp_path)
    settings.discussion_personas_path.write_bytes(b"garbage")
    with TestClient(app) as client:
        listing = client.get("/api/discussions/personas")
        assert listing.status_code == 200
        assert listing.json()["store"] == "corrupt" and listing.json()["custom"] == []
        assert client.get("/api/discussions/config").json()["personas_store"] == "corrupt"
        response = client.post("/api/discussions/personas", json=body(), headers=HEADERS)
        assert (response.status_code, response.json()) == (
            503,
            {"detail": "PERSONA_STORE_CORRUPT"},
        )
        start = client.post(
            "/api/discussions",
            json={"topic": TOPIC, "personas": ["調査係", "設計者"]},
            headers=HEADERS,
        )
        assert start.json() == {"detail": "PERSONA_STORE_CORRUPT"}
    assert settings.discussion_personas_path.read_bytes() == b"garbage"


def test_mutations_need_the_confirm_header_and_same_origin(tmp_path: Path) -> None:
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        path = "/api/discussions/personas"
        for method, url in (("post", path), ("put", path + "/x"), ("delete", path + "/x")):
            kwargs = {"json": body()} if method != "delete" else {}
            missing = getattr(client, method)(url, **kwargs)
            assert (missing.status_code, missing.json()["detail"]) == (
                403,
                "confirm_header_required",
            )
            cross = getattr(client, method)(
                url, headers={**HEADERS, "Origin": "http://evil.example"}, **kwargs
            )
            assert (cross.status_code, cross.json()["detail"]) == (403, "forbidden")
        assert client.get(path).json()["custom"] == []


def test_auth_is_required_for_persona_routes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    passphrase = "fake passphrase for tests only"
    settings = Settings(
        db_path=tmp_path / "db.sqlite3",
        discussion=True,
        model_choices=MODELS,
        discussion_personas_path=tmp_path / "personas.json",
        auth_passphrase_hash=hash_passphrase(passphrase, n=16, r=1, p=1),
        auth_signing_key="k" * 40,
        auth_cookie_secure=False,
    )
    auth = AuthService.from_settings(settings, limiter=LoginLimiter(failure_delay=0))
    app = create_app(settings, env.default, models=env.registry, auth=auth)
    with TestClient(app, base_url="http://testserver") as client:
        path = "/api/discussions/personas"
        assert client.get(path).status_code == 401
        assert client.post(path, json=body(), headers=HEADERS).status_code == 401
        assert client.put(path + "/x", json=body(), headers=HEADERS).status_code == 401
        assert client.delete(path + "/x", headers=HEADERS).status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"passphrase": passphrase},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        origin = {**HEADERS, "Origin": "http://testserver"}
        assert client.get(path).status_code == 200
        assert client.post(path, json=body(), headers=origin).status_code == 201


# ----- use: allowlist re-check, start by name, snapshot -----


def make_service(tmp_path: Path, env: Env, choices=MODELS) -> DiscussionService:
    return DiscussionService(
        env.service.queue,
        env.store,
        env.registry,
        env.default,
        model_choices=choices,
        max_rounds=3,
        per_day_limit=10,
        worker_poll_seconds=0.01,
        persona_store=PersonaStore(tmp_path / "personas.json"),
    )


def test_a_saved_persona_whose_model_disappears_is_unavailable_not_deleted(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = make_service(tmp_path, env)
    service.create_persona(body("調査係", model=MODELS[2]))
    assert service.personas()["custom"][0]["available"] is True
    env.ready = lambda provider: provider != "groq"
    listing = service.personas()
    assert listing["custom"][0]["available"] is False and listing["custom"][0]["name"] == "調査係"
    # Gone from the allowlist altogether: still listed, unavailable, and unusable.
    narrowed = make_service(tmp_path, env, choices=MODELS[:2])
    assert narrowed.personas()["custom"][0]["available"] is False

    async def attempt() -> str:
        with pytest.raises(DiscussionError) as caught:
            await narrowed.start(TOPIC, ["調査係", "設計者"], 1)
        return str(caught.value)

    assert asyncio.run(attempt()) == "PERSONA_MODEL_NOT_ALLOWED"
    assert narrowed._persona_store.get("調査係") is not None  # still saved


def test_start_accepts_a_saved_name_and_snapshots_it(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = make_service(tmp_path, env)
    service.create_persona(body("調査係", stance="元の立場", model=MODELS[1]))

    async def scenario() -> None:
        _, task_id = await service.start(TOPIC, ["調査係", "設計者"], 1)
        # Edited and deleted after the start: the queued discussion is not affected.
        service.replace_persona("調査係", body("調査係", stance="変更後の立場", model=MODELS[1]))
        service.delete_persona("調査係")
        stop = asyncio.Event()
        worker = asyncio.create_task(service.run_worker(stop))
        try:
            async with asyncio.timeout(10):
                while service.queue.repository.get_task(task_id).status.value in (
                    "pending",
                    "running",
                ):
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await worker

    asyncio.run(scenario())
    prompts = ["\n".join(m.content for m in r.messages) for r in env.providers[MODELS[1]].requests]
    assert prompts and "元の立場" in prompts[0] and "変更後の立場" not in prompts[0]
    assert env.providers[MODELS[1]].calls == 1


def test_unknown_name_is_an_unknown_preset(tmp_path: Path) -> None:
    env = Env(tmp_path)
    service = make_service(tmp_path, env)

    async def attempt() -> str:
        with pytest.raises(DiscussionError) as caught:
            await service.start(TOPIC, ["幽霊", "設計者"], 1)
        return str(caught.value)

    assert asyncio.run(attempt()) == "DISCUSSION_PRESET_UNKNOWN"


def test_no_persona_text_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _, _, app = make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/discussions/personas", json=body(), headers=HEADERS)
        client.post("/api/discussions/personas", json=body(), headers=HEADERS)  # duplicate
        client.put("/api/discussions/personas/x", json=body("y"), headers=HEADERS)
        client.get("/api/discussions/personas")
        client.delete(f"/api/discussions/personas/{quote('調査係')}", headers=HEADERS)
    text = "\n".join(
        record.getMessage() + " " + str(getattr(record, "__dict__", {}))
        for record in caplog.records
        if "discussion" in record.name or "persona" in record.getMessage()
    )
    assert SENTINEL not in text and "調査係" not in text


def test_the_schema_is_unchanged() -> None:
    assert SCHEMA_VERSION == 10
