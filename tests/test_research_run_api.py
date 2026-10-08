"""Starting, watching and cancelling a research over HTTP, with fakes only."""

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from research_run_support import (
    AGREEING_CLAIMS,
    AGREEING_HITS,
    AGREEING_PAGES,
    MARKER_TEXT,
    QUESTION,
    FakeSearch,
    Gate,
    ScriptedLLM,
    build_reader,
)

from backend.api.app import create_app
from backend.core.config import ConfigError, Settings
from backend.research.search_budget import BudgetedSearchProvider

pytestmark = pytest.mark.usefixtures("no_network")

ORIGIN = {"Origin": "http://testserver"}
FAKE_VALUE = "fake-value-for-tests-only"


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(db_path=tmp_path / "run-api.sqlite3", research_enabled=True, **overrides)


def make_client(
    tmp_path: Path,
    *,
    search=None,
    llm="default",
    pages=None,
    **overrides,
) -> TestClient:
    reader, _ = build_reader(AGREEING_PAGES if pages is None else pages)
    app = create_app(
        settings(tmp_path, **overrides),
        ScriptedLLM(AGREEING_CLAIMS) if llm == "default" else llm,
        search_provider=FakeSearch(AGREEING_HITS) if search is None else search,
        page_reader=reader,
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with make_client(tmp_path) as test_client:
        yield test_client


def start(client: TestClient, question: str = QUESTION, level: str = "quick", **kw):
    return client.post(
        "/api/research/sessions", json={"question": question, "level": level}, headers=ORIGIN, **kw
    )


def wait_status(client: TestClient, session_id: str, wanted: str, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/research/sessions/{session_id}").json()
        if detail["status"] == wanted:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"session never became {wanted}")


# ----- status and configuration -----


def test_status_reports_enabled_only_when_fully_configured(client: TestClient) -> None:
    assert client.get("/api/research/status").json() == {"enabled": True}


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"research_enabled": False}, "disabled"),
        ({"no_search": True}, "no_search_provider"),
        ({"no_chat": True}, "no_chat_provider"),
    ],
)
def test_missing_pieces_keep_research_off_with_a_fixed_reason(
    tmp_path: Path, kwargs: dict, reason: str
) -> None:
    reader, _ = build_reader(AGREEING_PAGES)
    app = create_app(
        Settings(
            db_path=tmp_path / "off.sqlite3", research_enabled=kwargs.get("research_enabled", True)
        ),
        None if kwargs.get("no_chat") else ScriptedLLM(AGREEING_CLAIMS),
        search_provider=None if kwargs.get("no_search") else FakeSearch(AGREEING_HITS),
        page_reader=reader,
    )
    with TestClient(app) as off:
        assert off.get("/api/research/status").json() == {"enabled": False, "reason": reason}
        for response in (
            start(off),
            off.post(f"/api/research/sessions/{uuid4()}/cancel", headers=ORIGIN),
        ):
            assert response.status_code == 503
            assert response.json() == {"detail": "research_not_configured"}
        assert off.get("/api/research/sessions").json() == {"sessions": []}


def test_the_switch_is_off_by_default(tmp_path: Path) -> None:
    assert Settings(db_path=tmp_path / "x.sqlite3").research_enabled is False
    app = create_app(
        Settings(db_path=tmp_path / "x.sqlite3"),
        ScriptedLLM(),
        search_provider=FakeSearch(),
    )
    with TestClient(app) as default:
        assert default.get("/api/research/status").json()["reason"] == "disabled"


def test_a_search_setting_without_httpx_support_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "backend.research.runner":
            raise ImportError("no httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(ConfigError):
        create_app(settings(tmp_path), ScriptedLLM(), search_provider=FakeSearch())


# ----- the happy path -----


def test_a_request_runs_to_a_cited_result(client: TestClient) -> None:
    response = start(client)
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"id"}
    detail = wait_status(client, body["id"], "completed")
    assert detail["level"] == "quick" and detail["failure_reason"] is None
    assert len(detail["claims"]) == 3
    source_ids = {source["id"] for source in detail["sources"]}
    assert all(claim["source_id"] in source_ids and claim["quote"] for claim in detail["claims"])
    assert "Verified claims:" in detail["result_text"]
    assert "progress" not in detail  # nothing live once it is finished


def test_standard_level_runs_and_lists_the_session(client: TestClient) -> None:
    session_id = start(client, level="standard").json()["id"]
    detail = wait_status(client, session_id, "completed")
    assert detail["level"] == "standard"
    listed = client.get("/api/research/sessions").json()["sessions"]
    assert [row["id"] for row in listed] == [session_id]


def test_progress_is_in_the_detail_while_running(tmp_path: Path) -> None:
    gate = Gate()
    with make_client(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate)) as live:
        session_id = start(live, level="standard").json()["id"]
        assert gate.wait_until_entered()
        detail = live.get(f"/api/research/sessions/{session_id}").json()
        assert detail["status"] == "running"
        assert detail["progress"]["stage"] == "searching"
        assert set(detail["progress"]) == {
            "stage", "round", "queries", "pages", "sources", "claims",
        }
        gate.release()
        wait_status(live, session_id, "completed")


# ----- busy, budget, cancel -----


def test_a_second_request_while_one_runs_is_busy_and_creates_nothing(tmp_path: Path) -> None:
    gate = Gate()
    with make_client(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate)) as live:
        first = start(live).json()["id"]
        assert gate.wait_until_entered()
        refused = start(live, question="a second question")
        assert refused.status_code == 429 and refused.json() == {"detail": "busy"}
        assert [row["id"] for row in live.get("/api/research/sessions").json()["sessions"]] == [
            first
        ]
        gate.release()
        wait_status(live, first, "completed")
        assert start(live, question="third").status_code == 202


def test_an_exhausted_local_budget_is_refused_before_queueing(tmp_path: Path) -> None:
    inner = FakeSearch(AGREEING_HITS)
    guarded = BudgetedSearchProvider(inner, monthly_limit=3, usage_counter=lambda: 3)
    with make_client(tmp_path, search=guarded) as limited:
        refused = start(limited)
        assert refused.status_code == 429
        assert refused.json() == {"detail": "search_budget_exhausted"}
        assert limited.get("/api/research/sessions").json() == {"sessions": []}
    assert inner.calls == []


def test_cancel_a_running_research(tmp_path: Path) -> None:
    gate = Gate()
    with make_client(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate)) as live:
        session_id = start(live, level="standard").json()["id"]
        assert gate.wait_until_entered()
        response = live.post(f"/api/research/sessions/{session_id}/cancel", headers=ORIGIN)
        assert response.status_code == 200
        assert response.json() == {"id": session_id, "status": "cancelling"}
        wait_status(live, session_id, "cancelled")
        again = live.post(f"/api/research/sessions/{session_id}/cancel", headers=ORIGIN)
        assert (again.status_code, again.json()) == (409, {"detail": "not_cancellable"})
        assert start(live, question="free again").status_code == 202


def test_cancel_errors_are_fixed_codes(client: TestClient) -> None:
    unknown = client.post(f"/api/research/sessions/{uuid4()}/cancel", headers=ORIGIN)
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "session_not_found"})
    malformed = client.post("/api/research/sessions/not-a-uuid/cancel", headers=ORIGIN)
    assert (malformed.status_code, malformed.json()) == (404, {"detail": "session_not_found"})
    assert "not-a-uuid" not in malformed.text


# ----- validation -----


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"question": "", "level": "quick"}, "question_required"),
        ({"question": "   \n\t ", "level": "quick"}, "question_required"),
        ({"question": None, "level": "quick"}, "question_required"),
        ({"question": 5, "level": "quick"}, "question_required"),
        ({"question": "x" * 2001, "level": "quick"}, "question_too_long"),
        ({"question": "hello\x00world", "level": "quick"}, "question_invalid_characters"),
        ({"question": "bell\x07", "level": "quick"}, "question_invalid_characters"),
        ({"question": "esc\x1b[31m", "level": "standard"}, "question_invalid_characters"),
        ({"question": "ok", "level": "deep"}, "invalid_level"),
        ({"question": "ok", "level": "extensive"}, "invalid_level"),
        ({"question": "ok", "level": "memory"}, "invalid_level"),
        ({"question": "ok", "level": "QUICK"}, "invalid_level"),
        ({"question": "ok", "level": 1}, "invalid_level"),
        ({"question": "ok"}, "invalid_body"),
        ({"level": "quick"}, "invalid_body"),
        ({"question": "ok", "level": "quick", "extra": 1}, "invalid_body"),
        (["question"], "invalid_body"),
    ],
)
def test_invalid_requests_get_a_fixed_422_and_create_nothing(
    client: TestClient, payload: object, code: str
) -> None:
    response = client.post("/api/research/sessions", json=payload, headers=ORIGIN)
    assert response.status_code == 422
    assert response.json() == {"detail": code}
    assert client.get("/api/research/sessions").json() == {"sessions": []}


def test_a_question_of_exactly_the_limit_and_with_newlines_is_accepted(client: TestClient) -> None:
    assert start(client, question="a" * 2000).status_code == 202


def test_validation_never_echoes_the_input(client: TestClient) -> None:
    hostile = f"<script>{MARKER_TEXT}</script>\x07"
    response = start(client, question=hostile)
    assert response.status_code == 422
    assert MARKER_TEXT not in response.text and "script" not in response.text
    broken = client.post(
        "/api/research/sessions",
        content=f'{{"question": "{MARKER_TEXT}", "level": ',
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert (broken.status_code, broken.json()) == (422, {"detail": "invalid_body"})
    assert MARKER_TEXT not in broken.text


def test_the_body_must_be_json_and_small(client: TestClient) -> None:
    text = client.post(
        "/api/research/sessions",
        content='{"question": "a", "level": "quick"}',
        headers={**ORIGIN, "Content-Type": "text/plain"},
    )
    assert (text.status_code, text.json()) == (415, {"detail": "unsupported_media_type"})
    big = client.post(
        "/api/research/sessions",
        content='{"question": "' + "a" * 20000 + '", "level": "quick"}',
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert (big.status_code, big.json()) == (413, {"detail": "invalid_body"})


def test_other_methods_are_405(client: TestClient) -> None:
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)("/api/research/sessions").status_code == 405
        cancel = f"/api/research/sessions/{uuid4()}/cancel"
        assert getattr(client, method)(cancel).status_code == 405
    assert client.post("/api/research/status", headers=ORIGIN).status_code == 405


# ----- same-origin -----


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "http://testserver:9999"},
        {"Origin": "null"},
        {"Referer": "http://evil.example/page"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_posts_are_refused_even_with_login_off(
    client: TestClient, headers: dict[str, str]
) -> None:
    created = client.post(
        "/api/research/sessions", json={"question": QUESTION, "level": "quick"}, headers=headers
    )
    assert (created.status_code, created.json()) == (403, {"detail": "forbidden"})
    cancelled = client.post(f"/api/research/sessions/{uuid4()}/cancel", headers=headers)
    assert (cancelled.status_code, cancelled.json()) == (403, {"detail": "forbidden"})
    assert client.get("/api/research/sessions").json() == {"sessions": []}


def test_cross_origin_is_refused_before_the_configuration_check(tmp_path: Path) -> None:
    app = create_app(Settings(db_path=tmp_path / "off.sqlite3"), ScriptedLLM())
    with TestClient(app) as off:
        response = off.post(
            "/api/research/sessions",
            json={"question": "q", "level": "quick"},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://testserver"},
        {"Referer": "http://testserver/research"},
        {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        {},  # not a browser: curl and tests carry no Origin
    ],
)
def test_same_origin_and_non_browser_posts_are_accepted(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/research/sessions", json={"question": QUESTION, "level": "quick"}, headers=headers
    )
    assert response.status_code == 202


def test_get_routes_do_not_need_an_origin(client: TestClient) -> None:
    for path in ("/api/research/status", "/api/research/sessions"):
        assert client.get(path, headers={"Origin": "http://evil.example"}).status_code == 200


# ----- failures, leakage -----


def test_a_failed_run_shows_only_the_fixed_reason(tmp_path: Path) -> None:
    from backend.research.reader import ReaderError, ReadFailure

    pages = {url: ReaderError(ReadFailure.NETWORK_ERROR) for url in AGREEING_PAGES}
    with make_client(tmp_path, pages=pages) as failing:
        session_id = start(failing, question=f"{QUESTION} {MARKER_TEXT}").json()["id"]
        detail = wait_status(failing, session_id, "failed")
        assert detail["failure_reason"] == "reader_failed"
        assert detail["result_text"] is None and detail["claims"] == []


def test_logs_and_responses_carry_no_key_question_or_vendor_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    leak_marker = "fake-key-" + MARKER_TEXT
    question = f"{QUESTION} {MARKER_TEXT}"
    with make_client(
        tmp_path,
        search=FakeSearch(AGREEING_HITS),
        llm=ScriptedLLM(error=RuntimeError(f"vendor says {leak_marker}")),
    ) as live:
        session_id = start(live, question=question).json()["id"]
        detail = wait_status(live, session_id, "failed")
        bodies = [
            str(detail["failure_reason"]),
            live.get("/api/research/status").text,
            start(live, question="\x07").text,
            live.post(f"/api/research/sessions/{uuid4()}/cancel", headers=ORIGIN).text,
        ]
    assert detail["failure_reason"] == "synthesis_failed"
    assert all(leak_marker not in body and MARKER_TEXT not in body for body in bodies)
    assert leak_marker not in caplog.text
    # The question is stored (it is the user's own data) but never written to the logs.
    assert MARKER_TEXT not in caplog.text
    assert "docs.a.test" not in caplog.text


def test_repr_of_settings_hides_the_search_key(tmp_path: Path) -> None:
    value = Settings(
        db_path=tmp_path / "k.sqlite3",
        search_provider="tavily",
        search_key=FAKE_VALUE,
        research_enabled=True,
    )
    assert FAKE_VALUE not in repr(value)


def test_the_worker_stops_with_the_app_and_recovers_on_start(tmp_path: Path) -> None:
    gate = Gate()
    with make_client(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate)) as first:
        session_id = start(first).json()["id"]
        assert gate.wait_until_entered()
    # The app shut down mid-run: the worker was cancelled and nothing keeps running.
    with make_client(tmp_path) as second:
        detail = second.get(f"/api/research/sessions/{session_id}").json()
        assert detail["status"] in ("cancelled", "failed")
        assert start(second, question="after restart").status_code == 202


def test_a_configured_tavily_provider_is_built_behind_the_budget_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    real = __import__("backend.api.app", fromlist=["create_search_provider"]).create_search_provider

    def spy(settings, *, repository=None, usage_counter=None):
        provider = real(settings, repository=repository, usage_counter=usage_counter)
        built.append(provider)
        return provider

    monkeypatch.setattr("backend.api.app.create_search_provider", spy)
    reader, _ = build_reader(AGREEING_PAGES)
    app = create_app(
        Settings(
            db_path=tmp_path / "tavily.sqlite3",
            research_enabled=True,
            search_provider="tavily",
            search_key=FAKE_VALUE,
            search_monthly_limit=5,
        ),
        ScriptedLLM(AGREEING_CLAIMS),
        page_reader=reader,
    )
    with TestClient(app) as live:
        assert live.get("/api/research/status").json() == {"enabled": True}
    assert len(built) == 1 and isinstance(built[0], BudgetedSearchProvider)
    assert built[0].monthly_limit == 5
