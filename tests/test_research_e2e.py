"""End to end: HTTP -> task queue -> runner -> Quick/Standard -> citations -> repository -> HTTP.

Everything is a fake (search provider, page transport behind the real safe reader, chat model,
router); no socket is opened. Each test drives the real path through ``create_app`` and reads the
outcome only through the public API, so what is asserted is what a user of the Research screen
sees. Lines marked ``known gap`` assert today's behaviour on purpose: when the gap is fixed the
assertion flips and the test is updated with it (see docs/research-e2e.md).
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from research_run_support import (
    AGREEING_CLAIMS,
    AGREEING_HITS,
    AGREEING_PAGES,
    QUESTION,
    SIXTY_A,
    SIXTY_B,
    SIXTY_C,
    URL_A,
    URL_B,
    URL_C,
    FakeSearch,
    Gate,
    ScriptedLLM,
    build_reader,
    foo_page,
    hit,
)

from backend.api.app import create_app
from backend.core.config import Settings
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.research.citations import normalize_space
from backend.research.reader import ReaderError, ReadFailure
from backend.research.search import SearchFailure
from backend.research.search_budget import BudgetedSearchProvider
from backend.research.standard import CAVEAT_TEXT, Caveat
from backend.router import Route, RouteDecision, RouteReason

pytestmark = pytest.mark.usefixtures("no_network")

ORIGIN = {"Origin": "http://testserver"}
ACTIVITY = {"X-Jarvis-Activity": "1"}
TWO_MIN = "The Foo widget cache keeps entries for 120 seconds."
URL_D = "https://docs.d.test/foo-cache-reference"
INVENTED = "The Foo widget cache is stored on a quantum disk."


def claim(text: str) -> tuple[str, str]:
    return (text, text)


class Env:
    """A running app over fakes, with the fakes kept for assertions."""

    def __init__(self, tmp_path: Path, *, search=None, pages=None, claims=None, llm=None, **kw):
        self.search = FakeSearch(AGREEING_HITS) if search is None else search
        self.pages = dict(AGREEING_PAGES if pages is None else pages)
        reader, self.transport = build_reader(self.pages)
        self.llm = llm or ScriptedLLM(AGREEING_CLAIMS if claims is None else claims)
        kw.setdefault("research_enabled", True)
        self.app = create_app(
            Settings(db_path=tmp_path / "e2e.sqlite3", **kw),
            self.llm,
            search_provider=self.search,
            page_reader=reader,
        )
        self.client = TestClient(self.app)

    def __enter__(self) -> "Env":
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    def start(self, level: str = "standard", question: str = QUESTION):
        return self.client.post(
            "/api/research/sessions", json={"question": question, "level": level}, headers=ORIGIN
        )

    def wait(self, session_id: str, wanted: str = "completed", timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        detail: dict = {}
        while time.monotonic() < deadline:
            detail = self.client.get(f"/api/research/sessions/{session_id}").json()
            if detail["status"] == wanted:
                return detail
            time.sleep(0.02)
        raise AssertionError(f"session never became {wanted}: {detail.get('status')}")

    def run(self, level: str = "standard", wanted: str = "completed") -> dict:
        response = self.start(level)
        assert response.status_code == 202
        return self.wait(response.json()["id"], wanted)


def result_lines(detail: dict) -> list[str]:
    return (detail["result_text"] or "").splitlines()


def by_url(detail: dict) -> dict[str, dict]:
    return {source["url"]: source for source in detail["sources"]}


def assert_citations_are_verified(env: Env, detail: dict) -> None:
    """Every claim cites a stored source and a quote that is word for word in that page."""
    sources = {source["id"]: source for source in detail["sources"]}
    for item in detail["claims"]:
        source = sources[item["source_id"]]
        page_text = env.pages[source["url"]]
        flat = normalize_space(
            page_text.replace("<p>", " ").replace("</p>", " ").replace("<h1>", " ")
        )
        assert item["quote"] and normalize_space(item["quote"]) in flat
        assert item["quote_start"] is not None and item["quote_end"] > item["quote_start"]
        assert item["claim_text"] in (detail["result_text"] or "")


# ----- success -----


@pytest.mark.parametrize("level", ["quick", "standard"])
def test_multiple_sources_give_a_cited_result_at_both_levels(tmp_path: Path, level: str) -> None:
    with Env(tmp_path) as env:
        detail = env.run(level)
    assert detail["level"] == level and detail["failure_reason"] is None
    assert len(detail["sources"]) == 3
    assert [c["claim_text"] for c in detail["claims"]] == [SIXTY_A, SIXTY_B, SIXTY_C]
    assert_citations_are_verified(env, detail)
    text = detail["result_text"]
    assert "Verified claims:" in text or "Standard research result" in text
    if level == "standard":
        # Standard writes the result from verified claims only; the model's prose is unused.
        assert "FREE TEXT THAT MUST NEVER APPEAR" not in text
    else:
        # known gap: Quick prints the model's own (unverified) prose above the verified claims.
        assert text.startswith("FREE TEXT THAT MUST NEVER APPEAR")
    assert "Caveats:" not in text and "Open conflicts" not in text
    assert detail["conflicts"] == [] and "progress" not in detail
    assert detail["queries"], "the queries that were run are recorded"
    listed = env.client.get("/api/research/sessions").json()["sessions"]
    assert [row["id"] for row in listed] == [detail["id"]] and listed[0]["has_result"] is True


def test_the_task_queue_carries_the_run_and_ends_completed(tmp_path: Path) -> None:
    with Env(tmp_path) as env:
        detail = env.run("standard")
        tasks = env.client.get("/api/tasks").json()
    items = tasks["tasks"] if isinstance(tasks, dict) else tasks
    mine = [t for t in items if detail["id"] in t["goal"]]
    assert len(mine) == 1 and mine[0]["status"] == "completed"
    assert QUESTION not in json.dumps(mine)  # the goal carries the id, never the question


# ----- cross-check -----


def test_agreeing_sources_are_corroborated_in_the_ratings(tmp_path: Path) -> None:
    with Env(tmp_path) as env:
        detail = env.run("standard")
    for source in detail["sources"]:
        assert source["evaluation"]["agreement"] == 1.0
        assert source["reasons"]["agreement"], "a fixed reason code is recorded"


def test_a_fact_found_in_one_source_only_stays_unverified_by_cross_check(tmp_path: Path) -> None:
    extra = "The Foo widget cache is flushed on every restart."
    pages = {**AGREEING_PAGES, URL_C: foo_page("Foo Cache News", SIXTY_C + " " + extra)}
    claims = {**AGREEING_CLAIMS, URL_C: [claim(SIXTY_C), claim(extra)]}
    with Env(tmp_path, pages=pages, claims=claims) as env:
        detail = env.run("standard")
    texts = {c["claim_text"] for c in detail["claims"]}
    assert extra in texts  # it is cited and verbatim ...
    sources = by_url(detail)
    # ... but no other source speaks to it, so the cross-check does not corroborate it, and
    # the agreement of the one source that carries it is not raised by it.
    assert sources[URL_C]["evaluation"]["agreement"] == 1.0  # from the 60 s claim, not the extra
    assert_citations_are_verified(env, detail)


def test_a_single_source_result_carries_the_few_sources_caveat(tmp_path: Path) -> None:
    only = {URL_A: AGREEING_PAGES[URL_A]}
    with Env(tmp_path, search=FakeSearch(AGREEING_HITS[:1]), pages=only) as env:
        detail = env.run("standard")
    assert [c["claim_text"] for c in detail["claims"]] == [SIXTY_A]
    assert CAVEAT_TEXT[Caveat.FEW_RELEVANT_SOURCES] in detail["result_text"]
    only_source = detail["sources"][0]
    assert only_source["evaluation"]["agreement"] is None
    assert "agreement_no_comparison" in only_source["reasons"]["agreement"]


# ----- conflicts -----


def test_conflicting_figures_are_flagged_and_both_sides_shown(tmp_path: Path) -> None:
    pages = {**AGREEING_PAGES, URL_B: foo_page("Foo Cache Notes", TWO_MIN)}
    claims = {**AGREEING_CLAIMS, URL_B: [claim(TWO_MIN)]}
    with Env(tmp_path, pages=pages, claims=claims) as env:
        detail = env.run("standard")
    assert detail["conflicts"]
    assert {c["kind"] for c in detail["conflicts"]} == {"number_mismatch"}
    assert all(c["status"] == "open" and c["resolution"] is None for c in detail["conflicts"])
    text = detail["result_text"]
    assert "Open conflicts" in text and "different figures" in text
    assert SIXTY_A in text and TWO_MIN in text  # neither side is dropped or preferred
    assert CAVEAT_TEXT[Caveat.UNRESOLVED_CONFLICTS] in text
    assert_citations_are_verified(env, detail)


# ----- unverifiable claims -----


def test_a_claim_with_an_invented_quote_is_dropped_and_reported(tmp_path: Path) -> None:
    claims = {**AGREEING_CLAIMS, URL_A: [claim(SIXTY_A), (INVENTED, "kept on a quantum disk")]}
    with Env(tmp_path, claims=claims) as env:
        detail = env.run("standard")
    assert INVENTED not in detail["result_text"]
    assert all(c["claim_text"] != INVENTED for c in detail["claims"])
    assert len(detail["claims"]) == 3
    assert CAVEAT_TEXT[Caveat.CLAIMS_REMOVED] in detail["result_text"]
    assert_citations_are_verified(env, detail)


def test_a_quote_from_the_wrong_source_is_not_accepted(tmp_path: Path) -> None:
    claims = {URL_A: [(SIXTY_B, SIXTY_B)], URL_B: [claim(SIXTY_B)], URL_C: [claim(SIXTY_C)]}
    with Env(tmp_path, claims=claims) as env:
        detail = env.run("standard")
    cited = [(c["claim_text"], c["source_id"]) for c in detail["claims"]]
    b_id = by_url(detail)[URL_B]["id"]
    assert [pair for pair in cited if pair[0] == SIXTY_B] == [(SIXTY_B, b_id)]


@pytest.mark.parametrize("level", ["quick", "standard"])
def test_when_nothing_can_be_verified_the_run_fails_with_no_answer(
    tmp_path: Path, level: str
) -> None:
    claims = {url: [(INVENTED, "kept on a quantum disk")] for url in AGREEING_PAGES}
    with Env(tmp_path, claims=claims) as env:
        detail = env.run(level, wanted="failed")
    assert detail["failure_reason"] == "synthesis_failed"
    assert detail["result_text"] is None and detail["claims"] == []


def test_an_honest_insufficient_evidence_reply_completes_without_claims(tmp_path: Path) -> None:
    with Env(tmp_path, llm=ScriptedLLM({}, insufficient=True)) as env:
        detail = env.run("standard")
    assert detail["claims"] == []
    assert "No claim could be verified against a source." in detail["result_text"]


# ----- limits and caveats -----


def test_unreadable_pages_become_a_caveat_and_never_a_claim(tmp_path: Path) -> None:
    pages = {**AGREEING_PAGES, URL_C: ReaderError(ReadFailure.HTTP_ERROR)}
    with Env(
        tmp_path, pages=pages, claims={URL_A: AGREEING_CLAIMS[URL_A], URL_B: AGREEING_CLAIMS[URL_B]}
    ) as env:
        detail = env.run("standard")
    assert "1 page(s) could not be read" in detail["result_text"]
    assert {s["url"] for s in detail["sources"]} == {URL_A, URL_B}
    assert URL_C in env.transport.requests


def test_quick_reads_at_most_three_pages(tmp_path: Path) -> None:
    urls = [f"https://docs.{n}.test/foo-cache" for n in "abcde"]
    pages = {u: foo_page(f"Foo {i}", SIXTY_A) for i, u in enumerate(urls)}
    hits = [hit(u, f"Foo {i}") for i, u in enumerate(urls)]
    claims = {u: [claim(SIXTY_A)] for u in urls}
    with Env(tmp_path, search=FakeSearch(hits), pages=pages, claims=claims) as env:
        detail = env.run("quick")
    assert len(env.transport.requests) == 3
    assert len(detail["sources"]) == 3


def test_standard_never_reads_more_than_its_page_budget(tmp_path: Path) -> None:
    urls = [f"https://docs.{n}.test/foo-cache" for n in "abcdefghijkl"]
    pages = {u: foo_page(f"Foo {i}", SIXTY_A) for i, u in enumerate(urls)}
    hits = [hit(u, f"Foo {i}") for i, u in enumerate(urls)]
    claims = {u: [claim(SIXTY_A)] for u in urls}
    with Env(tmp_path, search=FakeSearch(hits), pages=pages, claims=claims) as env:
        detail = env.run("standard")
    assert 1 <= len(env.transport.requests) <= 8
    assert detail["status"] == "completed"


# ----- search budget, provider failures -----


def test_an_exhausted_search_budget_is_refused_before_anything_is_queued(tmp_path: Path) -> None:
    inner = FakeSearch(AGREEING_HITS)
    guarded = BudgetedSearchProvider(inner, monthly_limit=2, usage_counter=lambda: 2)
    with Env(tmp_path, search=guarded) as env:
        refused = env.start("standard")
        assert (refused.status_code, refused.json()) == (429, {"detail": "search_budget_exhausted"})
        assert env.client.get("/api/research/sessions").json() == {"sessions": []}
    assert inner.calls == []


def test_a_budget_that_runs_out_mid_run_fails_closed(tmp_path: Path) -> None:
    inner = FakeSearch(AGREEING_HITS)
    state = {"first": True}

    def counter() -> int:  # fine at submit time, spent by the first search
        if state["first"]:
            state["first"] = False
            return 0
        return 99

    guarded = BudgetedSearchProvider(inner, monthly_limit=3, usage_counter=counter)
    with Env(tmp_path, search=guarded) as env:
        detail = env.run("standard", wanted="failed")
    assert detail["failure_reason"] == "search_failed"
    assert detail["result_text"] is None and detail["claims"] == []
    assert env.transport.requests == []


@pytest.mark.parametrize("level", ["quick", "standard"])
@pytest.mark.parametrize(
    "failure", [SearchFailure.UNAVAILABLE, SearchFailure.RATE_LIMITED, SearchFailure.TIMEOUT]
)
def test_a_failing_search_provider_never_produces_an_answer(
    tmp_path: Path, level: str, failure: SearchFailure
) -> None:
    with Env(tmp_path, search=FakeSearch(AGREEING_HITS, failure=failure)) as env:
        detail = env.run(level, wanted="failed")
    assert detail["failure_reason"] == "search_failed"
    assert detail["result_text"] is None and detail["claims"] == [] and detail["sources"] == []


@pytest.mark.parametrize("level", ["quick", "standard"])
def test_a_search_without_hits_is_no_results(tmp_path: Path, level: str) -> None:
    with Env(tmp_path, search=FakeSearch([])) as env:
        detail = env.run(level, wanted="failed")
    assert detail["failure_reason"] == "no_results" and detail["result_text"] is None


@pytest.mark.parametrize("level", ["quick", "standard"])
def test_a_failing_chat_provider_never_produces_an_answer(tmp_path: Path, level: str) -> None:
    llm = ScriptedLLM(error=RuntimeError("vendor exploded"))
    with Env(tmp_path, llm=llm) as env:
        detail = env.run(level, wanted="failed")
    assert detail["failure_reason"] == "synthesis_failed"
    assert detail["result_text"] is None and detail["claims"] == []
    assert "vendor exploded" not in json.dumps(detail)


def test_a_model_reply_that_is_not_json_is_a_failure_not_an_answer(tmp_path: Path) -> None:
    with Env(tmp_path, llm=ScriptedLLM(raw="Sure! The cache keeps entries for 60 seconds.")) as env:
        detail = env.run("standard", wanted="failed")
    assert detail["failure_reason"] == "synthesis_failed" and detail["result_text"] is None


def test_every_page_failing_is_reader_failed(tmp_path: Path) -> None:
    pages = {u: ReaderError(ReadFailure.NETWORK_ERROR) for u in AGREEING_PAGES}
    with Env(tmp_path, pages=pages) as env:
        detail = env.run("quick", wanted="failed")
    assert detail["failure_reason"] == "reader_failed" and detail["result_text"] is None


# ----- cancel -----


@pytest.mark.parametrize("level", ["quick", "standard"])
def test_cancel_stops_the_run_and_leaves_no_answer(tmp_path: Path, level: str) -> None:
    gate = Gate()
    with Env(tmp_path, search=FakeSearch(AGREEING_HITS, gate=gate)) as env:
        session_id = env.start(level).json()["id"]
        assert gate.wait_until_entered()
        response = env.client.post(f"/api/research/sessions/{session_id}/cancel", headers=ORIGIN)
        assert response.json() == {"id": session_id, "status": "cancelling"}
        detail = env.wait(session_id, "cancelled")
        assert detail["result_text"] is None and detail["claims"] == []
        assert env.llm.requests == []  # the model was never asked
        assert env.start(level, "free again").status_code == 202


# ----- SSRF -----


def test_internal_urls_from_a_search_are_never_fetched(tmp_path: Path) -> None:
    evil = [
        hit("http://169.254.169.254/latest/meta-data", "metadata"),
        hit("http://localhost/admin", "localhost"),
    ]
    with Env(tmp_path, search=FakeSearch(evil + AGREEING_HITS)) as env:
        detail = env.run("standard")
    requested = " ".join(env.transport.requests)
    for marker in ("169.254", "localhost"):
        assert marker not in requested
    stored = " ".join(s["url"] for s in detail["sources"])
    assert "169.254" not in stored and "localhost" not in stored
    assert len(detail["sources"]) == 3
    assert detail["status"] == "completed"


def test_known_gap_blocked_urls_use_up_the_page_budget(tmp_path: Path) -> None:
    evil = [
        hit("http://169.254.169.254/latest/meta-data", "metadata"),
        hit("http://127.0.0.1:8000/api/auth", "loopback"),
        hit("http://localhost/admin", "localhost"),
        hit("http://[::1]/", "ipv6 loopback"),
        hit("file:///etc/passwd", "file"),
    ]
    with Env(tmp_path, search=FakeSearch(evil + AGREEING_HITS)) as env:
        detail = env.run("standard")
    assert not any(m in " ".join(env.transport.requests) for m in ("169.254", "127.0.0.1", "::1"))
    # known gap: a blocked URL still counts as a page tried, so a flood of internal links in the
    # hits crowds good sources out of the first pass (5 slots): fewer than 3 sources survive.
    assert len(detail["sources"]) < 3


def test_only_internal_urls_means_failure_not_an_invented_answer(tmp_path: Path) -> None:
    evil = [hit("http://169.254.169.254/", "metadata"), hit("http://localhost/x", "local")]
    with Env(tmp_path, search=FakeSearch(evil)) as env:
        detail = env.run("quick", wanted="failed")
    assert env.transport.requests == []
    assert detail["failure_reason"] in ("reader_failed", "no_results")
    assert detail["result_text"] is None and detail["claims"] == []


# ----- regressions seen in the owner's real run (known gaps, current behaviour asserted) -----


def test_known_gap_near_duplicate_claims_are_all_kept(tmp_path: Path) -> None:
    reworded = "The Foo widget cache keeps its entries for 60 seconds."
    pages = {URL_A: foo_page("Foo Cache Docs", SIXTY_A + " " + reworded)}
    claims = {URL_A: [claim(SIXTY_A), claim(reworded)]}
    with Env(tmp_path, search=FakeSearch(AGREEING_HITS[:1]), pages=pages, claims=claims) as env:
        detail = env.run("standard")
    texts = [c["claim_text"] for c in detail["claims"]]
    # known gap: near-duplicate claims are not merged; the day they are, expect len(texts) == 1
    assert texts == [SIXTY_A, reworded]


def test_known_gap_one_domain_can_supply_every_source_without_a_caveat(tmp_path: Path) -> None:
    urls = [f"https://docs.a.test/foo-cache-{n}" for n in range(3)]
    pages = {u: foo_page(f"Foo Cache {n}", SIXTY_A) for n, u in enumerate(urls)}
    hits = [hit(u, f"Foo Cache {n}") for n, u in enumerate(urls)]
    claims = {u: [claim(SIXTY_A)] for u in urls}
    with Env(tmp_path, search=FakeSearch(hits), pages=pages, claims=claims) as env:
        detail = env.run("standard")
    assert len(detail["sources"]) == 3 and len(detail["claims"]) == 3
    assert all(s["evaluation"]["agreement"] == 1.0 for s in detail["sources"])
    # known gap: three pages of one domain count as three independent sources, so the result
    # says nothing about it; once a diversity caveat exists, expect it in result_text here.
    assert "Caveats:" not in detail["result_text"]


# ----- chat -> research -----


class Router:
    def __init__(self, route: Route = Route.research, confidence: float = 0.9) -> None:
        self.route, self.confidence = route, confidence

    async def decide(self, text: str) -> RouteDecision:
        return RouteDecision(self.route, self.confidence, RouteReason.model_choice, False)


class Combined:
    """Chat turns and research prompts share one provider, as in production."""

    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.research = ScriptedLLM(AGREEING_CLAIMS)
        self.chat_requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if any("<evidence" in m.content for m in request.messages):
            return await self.research.complete(request)
        self.chat_requests.append(request)
        return CompletionResponse("Acknowledged.", self.name, self.model)

    async def stream(self, request: CompletionRequest):
        self.chat_requests.append(request)
        yield "Acknowledged."


def chat_env(
    tmp_path: Path, *, router=None, search=None, **settings
) -> tuple[TestClient, Combined]:
    provider = Combined()
    reader, _ = build_reader(AGREEING_PAGES)
    settings.setdefault("research_enabled", True)
    app = create_app(
        Settings(db_path=tmp_path / "chat-e2e.sqlite3", **settings),
        provider,
        search_provider=FakeSearch(AGREEING_HITS) if search is None else search,
        page_reader=reader,
        router=router or Router(),
    )
    return TestClient(app), provider


def selected_event(text: str) -> dict:
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        payload = json.loads(data.removeprefix("data: "))
        if name == "event: activity" and payload.get("stage") == "route_selected":
            return payload
    raise AssertionError("no route_selected event")


def test_a_chat_message_starts_a_research_that_completes_with_citations(tmp_path: Path) -> None:
    client, provider = chat_env(tmp_path)
    with client:
        reply = client.post("/api/chat", json={"message": QUESTION}).json()["reply"]
        session_id = reply[reply.index("/research#") + 10 :][:36]
        deadline = time.monotonic() + 10
        detail = {}
        while time.monotonic() < deadline:
            detail = client.get(f"/api/research/sessions/{session_id}").json()
            if detail["status"] == "completed":
                break
            time.sleep(0.02)
    assert detail["status"] == "completed" and detail["question"] == QUESTION
    assert len(detail["claims"]) == 3 and detail["result_text"]
    assert provider.chat_requests == []  # the reply is fixed text, the model did not improvise


@pytest.mark.parametrize(
    ("case", "skip"),
    [
        ("low_confidence", "low_confidence"),
        ("budget", "budget_exhausted"),
        ("busy", "busy"),
    ],
)
def test_chat_falls_back_to_the_main_agent_and_says_why(
    tmp_path: Path, case: str, skip: str
) -> None:
    gate = Gate()
    kwargs: dict = {}
    if case == "low_confidence":
        kwargs["router"] = Router(confidence=0.3)
    elif case == "budget":
        kwargs["search"] = BudgetedSearchProvider(
            FakeSearch(AGREEING_HITS), monthly_limit=2, usage_counter=lambda: 2
        )
    elif case == "busy":
        kwargs["search"] = FakeSearch(AGREEING_HITS, gate=gate)
    client, provider = chat_env(tmp_path, **kwargs)
    with client:
        if case == "busy":
            client.post("/api/chat", json={"message": QUESTION})
            assert gate.wait_until_entered()
        text = client.post("/api/chat/stream", json={"message": QUESTION}, headers=ACTIVITY).text
        event = selected_event(text)
        gate.release()
    assert event["route"] == "main" and event["decided"] == "research"
    assert event["research_skip"] == skip
    assert "Acknowledged." in text and len(provider.chat_requests) == 1


def test_chat_with_research_switched_off_just_answers(tmp_path: Path) -> None:
    client, provider = chat_env(tmp_path, research_enabled=False)
    with client:
        text = client.post("/api/chat/stream", json={"message": QUESTION}, headers=ACTIVITY).text
        event = selected_event(text)
        assert client.get("/api/research/sessions").json() == {"sessions": []}
    assert event["route"] == "main" and "research_skip" not in event
    assert len(provider.chat_requests) == 1
