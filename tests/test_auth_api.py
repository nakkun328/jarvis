"""Login layer end to end: enforcement matrix, cookies, CSRF, SSE, limiter, logs, hostile input."""

import json
import re
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.auth.limiter import LoginLimiter
from backend.auth.passwords import hash_passphrase
from backend.auth.service import AuthService
from backend.core.config import Settings
from backend.core.database import Database
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.tasks.models import TaskStatus
from backend.tasks.repository import TaskRepository

PASSPHRASE = "fake passphrase for tests only"
WRONG = "not the right passphrase 000"
SIGNING = "k" * 40
HOST = "https://testserver"
HASH = hash_passphrase(PASSPHRASE, n=16, r=1, p=1)
ANOTHER_HASH = hash_passphrase("another fake passphrase 123", n=16, r=1, p=1)
REPLY = "FAKE-REPLY"


class FakeProvider:
    name = "fake"
    model = "fake-model"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(REPLY, self.name, self.model)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        yield "one "
        yield "two"


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, client: TestClient, auth: AuthService, clock: Clock, path: Path) -> None:
        self.client, self.auth, self.clock, self.path = client, auth, clock, path

    def post(self, url: str, origin: str | None = HOST, **kwargs):
        headers = {**kwargs.pop("headers", {})}
        if origin is not None:
            headers.setdefault("Origin", origin)
        return self.client.post(url, headers=headers, **kwargs)

    def login(self, passphrase: str = PASSPHRASE, **kwargs):
        return self.post("/api/auth/login", json={"passphrase": passphrase}, **kwargs)

    def token(self) -> str:
        response = self.login()
        assert response.status_code == 200
        value = response.cookies.get(self.auth.cookie_name)
        assert value
        self.client.cookies.clear()
        return value

    def cookie(self, token: str | None = None) -> dict[str, str]:
        return {"Cookie": f"{self.auth.cookie_name}={token or self.token()}"}


def make_env(
    tmp_path: Path,
    *,
    secure: bool = True,
    enabled: bool = True,
    trusted_proxy: bool = False,
    hours: int = 168,
    signing_key: str | None = SIGNING,
    passphrase_hash: str = HASH,
    limiter: LoginLimiter | None = None,
) -> Env:
    clock = Clock()
    settings = Settings(
        db_path=tmp_path / "auth.sqlite3",
        auth_passphrase_hash=passphrase_hash if enabled else None,
        auth_signing_key=signing_key,
        auth_session_hours=hours,
        auth_cookie_secure=secure,
        trusted_proxy=trusted_proxy,
    )
    auth = AuthService.from_settings(
        settings, clock=clock, limiter=limiter or LoginLimiter(failure_delay=0)
    )
    app = create_app(settings, FakeProvider(), auth=auth)
    client = TestClient(app, base_url=HOST if secure else "http://testserver")
    return Env(client, auth, clock, tmp_path / "auth.sqlite3")


@pytest.fixture
def open_env(tmp_path: Path) -> Iterator[Callable[..., Env]]:
    stack = ExitStack()

    def factory(**options: object) -> Env:
        built = make_env(tmp_path, **options)  # type: ignore[arg-type]
        stack.enter_context(built.client)  # runs startup so the database exists
        return built

    with stack:
        yield factory


@pytest.fixture
def env(open_env: Callable[..., Env]) -> Env:
    return open_env()


def cancelled_task_id(env: Env) -> str:
    repo = TaskRepository(Database(env.path))
    task = repo.create_task("goal", ["step"])
    repo.transition(task.id, TaskStatus.PENDING, TaskStatus.CANCELLED)
    return str(task.id)


def stderr_events(captured: str) -> list[dict[str, object]]:
    events = []
    for line in captured.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("logger") != "httpx":
            events.append(entry)
    return events


def app_log(captured: str) -> str:
    """Only JARVIS's own records: the test client's httpx logger prints URLs and is not ours."""
    return "\n".join(json.dumps(entry) for entry in stderr_events(captured))


# --- enforcement matrix ------------------------------------------------------------------------

PAGES = ["/", "/tasks", "/research", "/missing-page"]
API_GET = [
    "/health/ready",
    "/api/tasks",
    "/api/research/sessions",
    f"/api/tasks/{uuid4()}",
    f"/api/tasks/{uuid4()}/events",
    "/api/missing",
    "/api/auth",
    "/static/app.js",
    "/static/style.css",
    "/static/tasks.js",
]
API_WRITE = [("POST", "/api/chat"), ("POST", "/api/chat/stream"), ("POST", "/api/auth/logout")]
PUBLIC_GET = ["/login", "/static/login.css", "/static/login.js", "/health/live", "/api/auth/status"]


@pytest.mark.parametrize("path", PAGES)
def test_unauthenticated_pages_redirect_to_login(env: Env, path: str) -> None:
    for method in ("GET", "HEAD"):
        response = env.client.request(method, path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert response.text == "" or "login" in response.text.lower()


@pytest.mark.parametrize("path", API_GET)
def test_unauthenticated_api_gets_401_json_not_a_redirect(env: Env, path: str) -> None:
    response = env.client.get(path, follow_redirects=False)
    assert response.status_code == 401 and response.json() == {"detail": "unauthorized"}
    assert "location" not in response.headers


@pytest.mark.parametrize(("method", "path"), API_WRITE)
def test_unauthenticated_writes_get_401_even_with_a_good_origin(
    env: Env, method: str, path: str
) -> None:
    response = env.client.request(
        method, path, json={"message": "hi"}, headers={"Origin": HOST}, follow_redirects=False
    )
    assert response.status_code == 401 and response.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/", "/api/tasks", "/api/chat", "/login", "/api/auth/login"])
def test_other_methods_never_pass_without_a_session(env: Env, method: str, path: str) -> None:
    response = env.client.request(method, path, headers={"Origin": HOST}, follow_redirects=False)
    assert response.status_code in (401, 405)
    if path != "/login" and path != "/api/auth/login":
        assert response.status_code == 401


@pytest.mark.parametrize("path", PUBLIC_GET)
def test_public_routes_need_no_session(env: Env, path: str) -> None:
    assert env.client.get(path, follow_redirects=False).status_code == 200


def test_status_is_a_bare_boolean(env: Env) -> None:
    assert env.client.get("/api/auth/status").json() == {"authenticated": False}
    cookie = env.cookie()
    assert env.client.get("/api/auth/status", headers=cookie).json() == {"authenticated": True}
    junk = {"Cookie": f"{env.auth.cookie_name}=junk"}
    assert env.client.get("/api/auth/status", headers=junk).json() == {"authenticated": False}


@pytest.mark.parametrize("path", PAGES[:3])
def test_authenticated_pages_load(env: Env, path: str) -> None:
    response = env.client.get(path, headers=env.cookie(), follow_redirects=False)
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", API_GET[:3] + ["/static/app.js", "/static/style.css"])
def test_authenticated_api_and_assets_load(env: Env, path: str) -> None:
    assert env.client.get(path, headers=env.cookie()).status_code == 200


def test_authenticated_unknown_routes_are_404_not_auth_errors(env: Env) -> None:
    cookie = env.cookie()
    assert env.client.get("/missing-page", headers=cookie).status_code == 404
    assert env.client.get("/api/missing", headers=cookie).status_code == 404


def test_authenticated_chat_works_with_same_origin_post(env: Env) -> None:
    response = env.post("/api/chat", json={"message": "hello"}, headers=env.cookie())
    assert response.status_code == 200 and response.json()["reply"] == REPLY


@pytest.mark.parametrize(
    "path",
    [
        "/static/login.css/%2e%2e/app.js",
        "/static//login.css",
        "/static/login.css/",
        "/static/login.css%00.js",
        "/static/login.js.map",
        "/static/Login.css",
        "/login/",
        "/login/x",
        "//login",
        "/LOGIN",
        "/health/live/",
        "/health/live/x",
        "/health/ready",
        "/api/auth/status/",
        "/api/auth/login/",
        "/api/auth/logout",
    ],
)
def test_public_allowlist_is_exact(env: Env, path: str) -> None:
    response = env.client.get(path, follow_redirects=False)
    assert response.status_code in (401, 303, 405), path
    assert "text/css" not in response.headers.get("content-type", "")


def test_auth_disabled_changes_nothing(open_env: Callable[..., Env]) -> None:
    off = open_env(enabled=False)
    client = off.client
    for path in ("/", "/tasks", "/research", "/api/tasks", "/health/ready", "/static/app.js"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200, path
    assert client.post("/api/chat", json={"message": "hi"}).json()["reply"] == REPLY
    for path in ("/login", "/api/auth/status", "/static/login.js2"):
        assert client.get(path).status_code == 404
    assert client.post("/api/auth/login", json={"passphrase": PASSPHRASE}).status_code == 404
    assert "x-frame-options" not in client.get("/").headers
    assert not off.auth.enabled


# --- login, cookie, logout -----------------------------------------------------------------------


def parse_set_cookie(value: str) -> tuple[str, str, dict[str, str]]:
    name, _, rest = value.partition("=")
    token, *attributes = (part.strip() for part in rest.split(";"))
    flags = {}
    for attribute in attributes:
        key, _, val = attribute.partition("=")
        flags[key.lower()] = val
    return name, token, flags


def test_login_sets_a_hardened_cookie(env: Env) -> None:
    response = env.login()
    assert response.status_code == 200 and response.json() == {"authenticated": True}
    (header,) = response.headers.get_list("set-cookie")
    name, token, flags = parse_set_cookie(header)
    assert name == "__Host-jarvis_session"
    assert {"httponly", "secure"} <= flags.keys()
    assert flags["samesite"].lower() == "strict" and flags["path"] == "/"
    assert flags["max-age"] == str(168 * 3600) and "domain" not in flags
    assert PASSPHRASE not in header and token.count(".") == 1
    assert response.headers["cache-control"] == "no-store"


def test_cookie_without_secure_is_allowed_only_by_explicit_config(
    open_env: Callable[..., Env],
) -> None:
    local = open_env(secure=False, hours=2)
    (header,) = local.login().headers.get_list("set-cookie")
    name, _, flags = parse_set_cookie(header)
    assert name == "jarvis_session" and "secure" not in flags
    assert {"httponly"} <= flags.keys() and flags["samesite"].lower() == "strict"
    assert flags["max-age"] == "7200"


def test_cookie_jar_flow_login_use_logout(env: Env) -> None:
    assert env.client.get("/api/tasks").status_code == 401
    assert env.login().status_code == 200
    assert env.client.get("/api/tasks").status_code == 200
    response = env.post("/api/auth/logout")
    assert response.status_code == 200 and response.json() == {"authenticated": False}
    (header,) = response.headers.get_list("set-cookie")
    name, token, flags = parse_set_cookie(header)
    assert token in ('""', "") and flags["max-age"] == "0"
    assert {"httponly", "secure", "path"} <= flags.keys()
    assert env.client.get("/api/tasks").status_code == 401


def test_logout_needs_a_session_and_a_same_origin_request(env: Env) -> None:
    assert env.post("/api/auth/logout").status_code == 401
    cookie = env.cookie()
    assert env.post("/api/auth/logout", origin=None, headers=cookie).status_code == 403
    assert (
        env.post("/api/auth/logout", origin="https://evil.example", headers=cookie).status_code
        == 403
    )
    assert env.post("/api/auth/logout", headers=cookie).status_code == 200


def test_logout_is_client_side_only_so_a_copied_cookie_lives_until_expiry(env: Env) -> None:
    # Known limitation of stateless sessions (docs/auth.md): rotate the signing key to revoke.
    cookie = env.cookie()
    assert env.post("/api/auth/logout", headers=cookie).status_code == 200
    assert env.client.get("/api/tasks", headers=cookie).status_code == 200


def test_wrong_passphrase_is_a_generic_401_without_cookie(env: Env) -> None:
    response = env.login(WRONG)
    assert response.status_code == 401 and response.json() == {"detail": "invalid credentials"}
    assert "set-cookie" not in response.headers
    assert WRONG not in response.text and PASSPHRASE not in response.text


def test_session_expires_at_its_absolute_lifetime(open_env: Callable[..., Env]) -> None:
    short = open_env(hours=1)
    cookie = short.cookie()
    short.clock.now += 3599
    assert short.client.get("/api/tasks", headers=cookie).status_code == 200
    short.clock.now += 1
    assert short.client.get("/api/tasks", headers=cookie).status_code == 401


def test_requests_do_not_extend_the_session(open_env: Callable[..., Env]) -> None:
    short = open_env(hours=1)
    cookie = short.cookie()
    for _ in range(6):
        short.clock.now += 700
        short.client.get("/api/tasks", headers=cookie)
    assert short.client.get("/api/tasks", headers=cookie).status_code == 401


def test_changing_the_signing_key_or_passphrase_logs_everything_out(
    open_env: Callable[..., Env], tmp_path: Path
) -> None:
    first = open_env()
    cookie = first.cookie()
    for changes in ({"signing_key": "z" * 40}, {"passphrase_hash": ANOTHER_HASH}):
        other = make_env(tmp_path, **changes)
        with other.client:
            assert other.client.get("/api/tasks", headers=cookie).status_code == 401


def test_tampered_and_garbage_cookies_are_refused(env: Env) -> None:
    token = env.token()
    payload, signature = token.split(".")
    name = env.auth.cookie_name
    candidates = [
        f"{payload}.{signature[:-2]}AA",
        f"{payload}x.{signature}",
        token + "=",
        "",
        ".",
        "a" * 3000,
        '""',
        f"{name}={token}",
        token.replace(".", ","),
    ]
    for value in candidates:
        response = env.client.get("/api/tasks", headers={"Cookie": f"{name}={value}"})
        assert response.status_code == 401, value[:20]
    # Wrong cookie name (the insecure name when Secure is configured) is ignored.
    other = {"Cookie": f"jarvis_session={token}"}
    assert env.client.get("/api/tasks", headers=other).status_code == 401


def test_multiple_cookie_headers_and_pairs(env: Env) -> None:
    token = env.token()
    name = env.auth.cookie_name
    ok = {"Cookie": f"junk=1; {name}=bad; other=2; {name}={token}"}
    assert env.client.get("/api/tasks", headers=ok).status_code == 200
    huge = {"Cookie": f"a={'x' * 9000}; {name}={token}"}
    assert env.client.get("/api/tasks", headers=huge).status_code == 401


# --- CSRF ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"Origin": "https://testserver"}, True),
        ({"Origin": "https://TESTSERVER"}, True),
        ({"Origin": "https://testserver:443"}, True),
        ({"Referer": "https://testserver/"}, True),
        ({"Referer": "https://testserver/tasks?x=1"}, True),
        ({}, False),
        ({"Origin": "null"}, False),
        ({"Origin": ""}, False),
        ({"Origin": "https://evil.example"}, False),
        ({"Origin": "https://testserver.evil.example"}, False),
        ({"Origin": "https://evil.example/https://testserver"}, False),
        ({"Origin": "https://testserver@evil.example"}, False),
        ({"Origin": "https://evil.example@testserver"}, False),
        ({"Origin": "https://testserver:8443"}, False),
        ({"Origin": "ftp://testserver"}, False),
        ({"Origin": "testserver"}, False),
        ({"Origin": "//testserver"}, False),
        ({"Origin": "https://[::1"}, False),
        ({"Origin": "https://evil.example", "Referer": "https://testserver/"}, False),
        ({"Referer": "https://evil.example/"}, False),
        ({"Referer": "not a url"}, False),
    ],
)
def test_state_changing_requests_need_a_same_origin_header(
    env: Env, headers: dict[str, str], allowed: bool
) -> None:
    response = env.client.post(
        "/api/chat", json={"message": "hi"}, headers={**env.cookie(), **headers}
    )
    assert response.status_code == (200 if allowed else 403)
    if not allowed:
        assert response.json() == {"detail": "forbidden"}


def test_duplicate_origin_headers_are_refused(env: Env) -> None:
    headers = [("Origin", HOST), ("Origin", HOST), ("Cookie", env.cookie()["Cookie"])]
    response = env.client.post("/api/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_is_origin_checked(env: Env, method: str) -> None:
    response = env.client.request(method, "/api/tasks", headers=env.cookie())
    assert response.status_code == 403


def test_origin_must_match_the_port_in_the_host_header(env: Env) -> None:
    cookie = env.cookie()
    ok = {**cookie, "Host": "testserver:8443", "Origin": "https://testserver:8443"}
    assert env.client.post("/api/chat", json={"message": "x"}, headers=ok).status_code == 200
    bad = {**cookie, "Host": "testserver:8443", "Origin": "https://testserver"}
    assert env.client.post("/api/chat", json={"message": "x"}, headers=bad).status_code == 403


def test_forwarded_host_is_used_only_for_a_trusted_proxy(open_env: Callable[..., Env]) -> None:
    plain = open_env()
    cookie = plain.cookie()
    headers = {**cookie, "Origin": "https://jarvis.example", "X-Forwarded-Host": "jarvis.example"}
    assert plain.client.post("/api/chat", json={"message": "x"}, headers=headers).status_code == 403


def test_trusted_proxy_forwarded_host(tmp_path: Path) -> None:
    proxied = make_env(tmp_path, trusted_proxy=True)
    with proxied.client:
        cookie = proxied.cookie()
        headers = {
            **cookie,
            "Origin": "https://jarvis.example",
            "X-Forwarded-Host": "jarvis.example",
        }
        assert (
            proxied.client.post("/api/chat", json={"message": "x"}, headers=headers).status_code
            == 200
        )
        evil = {**cookie, "Origin": "https://evil.example", "X-Forwarded-Host": "jarvis.example"}
        assert (
            proxied.client.post("/api/chat", json={"message": "x"}, headers=evil).status_code == 403
        )


def test_login_itself_is_origin_checked(env: Env) -> None:
    body = {"passphrase": PASSPHRASE}
    assert env.client.post("/api/auth/login", json=body).status_code == 403
    assert env.post("/api/auth/login", origin="https://evil.example", json=body).status_code == 403
    assert (
        env.client.post(
            "/api/auth/login", json=body, headers={"Referer": HOST + "/login"}
        ).status_code
        == 200
    )


def test_safe_methods_need_no_origin(env: Env) -> None:
    assert env.client.get("/api/tasks", headers=env.cookie()).status_code == 200


# --- SSE -----------------------------------------------------------------------------------------


def test_chat_stream_works_with_cookie_and_is_refused_without(env: Env) -> None:
    body = {"message": "hi"}
    assert env.post("/api/chat/stream", json=body).status_code == 401
    response = env.post("/api/chat/stream", json=body, headers=env.cookie())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"  # the route's own value is kept
    assert "event: delta" in response.text and "event: done" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


def test_task_stream_works_with_cookie_and_is_refused_without(env: Env) -> None:
    task_id = cancelled_task_id(env)
    url = f"/api/tasks/{task_id}/events"
    denied = env.client.get(url)
    assert denied.status_code == 401 and "event:" not in denied.text
    response = env.client.get(url, headers=env.cookie())
    assert response.status_code == 200 and "event: snapshot" in response.text
    assert "event: done" in response.text


def test_stream_requires_the_origin_check_for_post_streams(env: Env) -> None:
    response = env.client.post("/api/chat/stream", json={"message": "hi"}, headers=env.cookie())
    assert response.status_code == 403


def test_websocket_without_session_is_closed(env: Env) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), env.client.websocket_connect("/ws"):
        pass


# --- security headers ----------------------------------------------------------------------------


def hardened(response) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert (
        "no-store" in response.headers["cache-control"]
        or "no-cache" in response.headers["cache-control"]
    )


def test_every_kind_of_response_carries_the_headers(env: Env) -> None:
    cookie = env.cookie()
    for response in (
        env.client.get("/", follow_redirects=False),
        env.client.get("/api/tasks"),
        env.client.get("/login"),
        env.client.get("/static/login.js"),
        env.client.get("/health/live"),
        env.client.get("/api/auth/status"),
        env.client.get("/", headers=cookie),
        env.client.get("/api/tasks", headers=cookie),
        env.client.post("/api/chat", json={"message": "x"}),
        env.client.get("/missing", headers=cookie),
    ):
        hardened(response)
    for response in (
        env.client.get("/api/auth/status"),
        env.client.get("/", headers=cookie),
        env.client.get("/", follow_redirects=False),
        env.login(WRONG),
    ):
        assert response.headers["cache-control"] == "no-store"


def test_login_page_is_self_contained_and_script_policy_is_strict(env: Env) -> None:
    response = env.client.get("/login")
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert "frame-ancestors 'none'" in policy and "default-src 'none'" in policy
    html = response.text
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html) and "javascript:" not in html
    assert 'name="passphrase"' not in html and "name=" not in re.search(
        r"<input[^>]*>", html, re.S
    ).group(0)
    assert "<a " not in html
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert ref in ("/static/login.css", "/static/login.js")


def test_login_assets_avoid_dangerous_sinks() -> None:
    source = (Path(__file__).resolve().parents[1] / "frontend" / "login.js").read_text("utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write"):
        assert sink not in source
    assert "localStorage" not in source and "sessionStorage" not in source
    assert "textContent" in source


# --- brute force ---------------------------------------------------------------------------------


def test_lockout_after_five_failures_then_exponential_backoff(open_env: Callable[..., Env]) -> None:
    clock = Clock(5000.0)
    lim = LoginLimiter(clock=clock, failure_delay=0)
    guarded = open_env(limiter=lim)
    for _ in range(5):
        assert guarded.login(WRONG).status_code == 401
    locked = guarded.login(PASSPHRASE)  # even the right passphrase is refused while locked
    assert locked.status_code == 429 and locked.headers["retry-after"] == "30"
    assert locked.json() == {"detail": "too many attempts"} and "set-cookie" not in locked.headers
    clock.now += 31
    assert guarded.login(WRONG).status_code == 401
    again = guarded.login(WRONG)
    assert again.status_code == 429 and again.headers["retry-after"] == "60"
    clock.now += 61
    assert guarded.login(PASSPHRASE).status_code == 200
    for _ in range(5):  # a success starts the count over
        assert guarded.login(WRONG).status_code == 401


def test_malformed_requests_neither_consume_attempts_nor_reset_the_count(
    open_env: Callable[..., Env],
) -> None:
    clock = Clock()
    guarded = open_env(limiter=LoginLimiter(clock=clock, failure_delay=0))
    for _ in range(4):
        assert guarded.login(WRONG).status_code == 401
    for _ in range(20):
        assert guarded.post("/api/auth/login", json=["x"]).status_code == 400
    assert guarded.login(WRONG).status_code == 401  # fifth failure
    assert guarded.login(WRONG).status_code == 429


def test_failed_login_waits_a_fixed_delay(
    open_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    import backend.auth.routes as routes

    monkeypatch.setattr(routes.asyncio, "sleep", fake_sleep)
    delayed = open_env(limiter=LoginLimiter())
    assert delayed.login(WRONG).status_code == 401
    assert delayed.login().status_code == 200
    assert waits == [0.5]


def test_peer_address_decides_and_forwarded_for_is_ignored_by_default(
    open_env: Callable[..., Env],
) -> None:
    guarded = open_env()
    for i in range(5):
        spoof = {"X-Forwarded-For": f"203.0.113.{i}"}
        assert guarded.login(WRONG, headers=spoof).status_code == 401
    assert guarded.login(WRONG, headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 429


def test_trusted_proxy_uses_the_last_forwarded_entry(tmp_path: Path) -> None:
    proxied = make_env(tmp_path, trusted_proxy=True)
    with proxied.client:
        # The client controls everything left of the proxy's own entry, so rotating the first
        # value does not escape the lockout.
        for i in range(5):
            headers = {"X-Forwarded-For": f"203.0.113.{i}, 198.51.100.7"}
            assert proxied.login(WRONG, headers=headers).status_code == 401
        same = {"X-Forwarded-For": "203.0.113.200, 198.51.100.7"}
        assert proxied.login(WRONG, headers=same).status_code == 429
        other = {"X-Forwarded-For": "203.0.113.1, 198.51.100.8"}
        assert proxied.login(WRONG, headers=other).status_code == 401
        # Garbage or empty values fall back to the peer address instead of failing open.
        for junk in ("garbage", "", "999.1.1.1", ", "):
            assert proxied.login(WRONG, headers={"X-Forwarded-For": junk}).status_code in (401, 429)


def test_ipv6_clients_share_a_prefix_bucket(tmp_path: Path) -> None:
    proxied = make_env(tmp_path, trusted_proxy=True)
    with proxied.client:
        for i in range(5):
            headers = {"X-Forwarded-For": f"2001:db8:1:2::{i:x}"}
            assert proxied.login(WRONG, headers=headers).status_code == 401
        assert (
            proxied.login(WRONG, headers={"X-Forwarded-For": "2001:db8:1:2:ffff::9"}).status_code
            == 429
        )
        assert (
            proxied.login(WRONG, headers={"X-Forwarded-For": "2001:db8:1:3::1"}).status_code == 401
        )


def test_global_cap_stops_distributed_guessing(tmp_path: Path) -> None:
    clock = Clock()
    lim = LoginLimiter(clock=clock, failure_delay=0, global_limit=8)
    proxied = make_env(tmp_path, trusted_proxy=True, limiter=lim)
    with proxied.client:
        for i in range(8):
            headers = {"X-Forwarded-For": f"203.0.113.{i}"}
            assert proxied.login(WRONG, headers=headers).status_code == 401
        fresh = proxied.login(PASSPHRASE, headers={"X-Forwarded-For": "203.0.113.250"})
        assert fresh.status_code == 429 and int(fresh.headers["retry-after"]) > 0
        clock.now += 901
        assert (
            proxied.login(PASSPHRASE, headers={"X-Forwarded-For": "203.0.113.250"}).status_code
            == 200
        )


def test_a_limited_client_can_still_use_an_existing_session(env: Env) -> None:
    cookie = env.cookie()
    for _ in range(6):
        env.login(WRONG)
    assert env.client.get("/api/tasks", headers=cookie).status_code == 200


# --- hostile login input ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"passphrase": None},
        {"passphrase": 123},
        {"passphrase": ["a"]},
        {"passphrase": {"a": 1}},
        {"passphrase": True},
        {"pass": PASSPHRASE},
        {"passphrase": "x" * 2000},
        [PASSPHRASE],
        PASSPHRASE,
        None,
        5,
    ],
)
def test_wrong_shapes_are_400_without_details(env: Env, payload: object) -> None:
    response = env.post("/api/auth/login", json=payload)
    assert response.status_code == 400 and response.json() == {"detail": "invalid request"}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"\xff\xfe",
        b"null",
        b"[" * 4000,
        b'{"passphrase": "a"' + b"}" * 3,
        b"{'passphrase': 'x'}",
    ],
)
def test_garbage_bodies_are_400(env: Env, raw: bytes) -> None:
    response = env.post(
        "/api/auth/login", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""]
)
def test_only_json_is_accepted(env: Env, content_type: str) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    body = f"passphrase={PASSPHRASE}".encode()
    response = env.post("/api/auth/login", content=body, headers=headers)
    assert response.status_code == 400 and "set-cookie" not in response.headers


def test_json_content_type_with_charset_is_fine(env: Env) -> None:
    body = json.dumps({"passphrase": PASSPHRASE}).encode()
    headers = {"Content-Type": "Application/JSON; charset=utf-8"}
    assert env.post("/api/auth/login", content=body, headers=headers).status_code == 200


def test_huge_bodies_are_refused_early(env: Env) -> None:
    big = json.dumps({"passphrase": "a" * 50_000}).encode()
    headers = {"Content-Type": "application/json"}
    response = env.post("/api/auth/login", content=big, headers=headers)
    assert response.status_code == 413 and response.json() == {"detail": "request too large"}

    def chunks():  # no Content-Length: chunked upload must be cut off while reading
        for _ in range(100):
            yield b" " * 1000

    assert env.post("/api/auth/login", content=chunks(), headers=headers).status_code == 413


@pytest.mark.parametrize(
    "passphrase",
    [
        "",
        " ",
        "\u0000",
        "pass\u0000phrase",
        "日本語のパスフレーズ",
        "😀" * 50,
        "a\nb",
        "\u202e" * 20,
    ],
)
def test_odd_unicode_and_null_bytes_fail_cleanly(env: Env, passphrase: str) -> None:
    response = env.post("/api/auth/login", json={"passphrase": passphrase})
    assert response.status_code == 401 and response.json() == {"detail": "invalid credentials"}


def test_a_surrogate_in_raw_json_does_not_crash(env: Env) -> None:
    raw = b'{"passphrase": "\\ud800x"}'
    response = env.post(
        "/api/auth/login", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code in (400, 401)


def test_normalized_equivalent_input_logs_in(open_env: Callable[..., Env]) -> None:
    other = open_env()
    # NFKC folds the full-width form to the ASCII passphrase the hash was made from.
    wide = PASSPHRASE.translate({ord(c): ord(c) + 0xFEE0 for c in "abcdefghijklmnopqrstuvwxyz"})
    assert other.login(wide).status_code == 200


# --- logging -----------------------------------------------------------------------------------


def test_logs_never_contain_passphrases_hashes_tokens_or_addresses(
    open_env: Callable[..., Env], capsys: pytest.CaptureFixture[str]
) -> None:
    logged = open_env(limiter=LoginLimiter(failure_delay=0))
    assert logged.login(WRONG).status_code == 401
    token = logged.token()
    logged.client.get("/api/tasks", headers={"Cookie": f"{logged.auth.cookie_name}={token}"})
    logged.post("/api/auth/logout", headers={"Cookie": f"{logged.auth.cookie_name}={token}"})
    for _ in range(6):
        logged.login(WRONG)
    raw = app_log(capsys.readouterr().err)
    for forbidden in (
        PASSPHRASE,
        WRONG,
        HASH,
        SIGNING,
        token,
        token.split(".")[0],
        HASH.split("$")[-1],
    ):
        assert forbidden not in raw
    assert "testclient" not in raw
    events = [str(entry["event"]) for entry in stderr_events(raw)]
    for fixed in ("auth.login.failure", "auth.login.success", "auth.login.locked", "auth.logout"):
        assert fixed in events
    clients = {e["client"] for e in stderr_events(raw) if "client" in e}
    assert all(re.fullmatch(r"[0-9a-f]{12}", str(c)) for c in clients) and len(clients) == 1


def test_request_log_for_a_refusal_has_no_credentials(
    open_env: Callable[..., Env], capsys: pytest.CaptureFixture[str]
) -> None:
    env = open_env()
    secret_cookie = {"Cookie": f"{env.auth.cookie_name}=sentinel-cookie-value"}
    env.client.get("/api/tasks?q=sentinel-query", headers=secret_cookie)
    raw = app_log(capsys.readouterr().err)
    assert "sentinel-cookie-value" not in raw and "sentinel-query" not in raw
    (record,) = [e for e in stderr_events(raw) if e["event"] == "http.request"]
    assert record["status"] == 401 and record["path"] == "/api/tasks"


def test_ephemeral_signing_key_is_generated_logged_once_and_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "EPHEMERAL-KEY-MARKER-" + "x" * 30
    import backend.auth.service as service

    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda _n=None: marker)
    local = make_env(tmp_path, signing_key=None)
    assert local.auth.ephemeral_key
    with local.client:
        cookie = local.cookie()
        assert local.client.get("/api/tasks", headers=cookie).status_code == 200
    raw = app_log(capsys.readouterr().err)
    assert marker not in raw
    warnings = [e for e in stderr_events(raw) if str(e["event"]).startswith("auth.signing_key")]
    assert len(warnings) == 1 and "restart" in str(warnings[0]["event"])


def test_ephemeral_keys_differ_per_process_so_sessions_reset(tmp_path: Path) -> None:
    first = make_env(tmp_path, signing_key=None)
    second = make_env(tmp_path, signing_key=None)
    with first.client, second.client:
        cookie = first.cookie()
        assert first.client.get("/api/tasks", headers=cookie).status_code == 200
        assert second.client.get("/api/tasks", headers=cookie).status_code == 401


def test_explicit_key_survives_a_restart(tmp_path: Path) -> None:
    first = make_env(tmp_path)
    second = make_env(tmp_path)
    with first.client, second.client:
        cookie = first.cookie()
        assert second.client.get("/api/tasks", headers=cookie).status_code == 200


def test_insecure_cookie_setting_is_called_out_in_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = make_env(tmp_path, secure=False)
    with local.client:
        pass
    events = [str(e["event"]) for e in stderr_events(capsys.readouterr().err)]
    assert any(e.startswith("auth.cookie.insecure") for e in events)
    assert "auth.enabled" in events
