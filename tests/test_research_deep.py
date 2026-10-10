"""Deep research: plan, multi-round run, budgets, cancel, time limit, levels.

Fakes only (search, pages behind the real safe reader, scripted model); no socket is opened.
"""

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from research_run_support import (
    NOW,
    QUESTION,
    FakeSearch,
    Gate,
    Harness,
    ScriptedLLM,
    build_reader,
    foo_page,
    hit,
)

from backend.api.app import create_app
from backend.core.config import Settings
from backend.providers.base import CompletionRequest, CompletionResponse
from backend.research import deep
from backend.research.deep import (
    DEEP_MAX_PAGES,
    DEEP_MAX_SEARCH_QUERIES,
    DeepLimits,
    DeepResearch,
    PlanSource,
    fallback_plan,
    plan_sub_questions,
    validate_plan,
)
from backend.research.models import FailureReason, ResearchLevel, ResearchStatus
from backend.research.reuse import ReuseReason, decide_reuse
from backend.research.run_control import REQUESTABLE_LEVELS
from backend.research.search import SearchError, SearchFailure, SearchQuery
from backend.research.search_budget import BudgetedSearchProvider
from backend.research.standard import CAVEAT_TEXT, Caveat

pytestmark = pytest.mark.usefixtures("no_network")

ORIGIN = {"Origin": "http://testserver"}
SUB_LIFE = "Foo widget cache entry lifetime"
SUB_EVICT = "Foo widget cache eviction order"
LIFE_A = "The Foo widget cache keeps entries for 60 seconds."
LIFE_B = "Foo widget cache entries are kept for 60 seconds."
EVICT_C = "The Foo widget cache evicts the oldest entry first."
EVICT_D = "Foo widget cache eviction removes the oldest entry first."
URL_LA = "https://docs.a.test/foo-cache-life"
URL_LB = "https://docs.b.test/foo-cache-life"
URL_EC = "https://docs.c.test/foo-cache-evict"
URL_ED = "https://docs.d.test/foo-cache-evict"
URL_X = "https://docs.x.test/foo-cache-extra"
EXTRA = "The Foo widget cache can be cleared by an administrator."
PAGES = {
    URL_LA: foo_page("Foo Cache Life A", LIFE_A),
    URL_LB: foo_page("Foo Cache Life B", LIFE_B),
    URL_EC: foo_page("Foo Cache Evict C", EVICT_C),
    URL_ED: foo_page("Foo Cache Evict D", EVICT_D),
    URL_X: foo_page("Foo Cache Extra", EXTRA),
}
CLAIMS = {
    URL_LA: [(LIFE_A, LIFE_A)],
    URL_LB: [(LIFE_B, LIFE_B)],
    URL_EC: [(EVICT_C, EVICT_C)],
    URL_ED: [(EVICT_D, EVICT_D)],
    URL_X: [(EXTRA, EXTRA)],
}
HITS = {
    SUB_LIFE: [hit(URL_LA, "Foo Cache Life A")],
    SUB_EVICT: [hit(URL_EC, "Foo Cache Evict C")],
}
EXTRA_HITS = [hit(URL_X, "Foo Cache Extra")]
PLAN = json.dumps({"sub_questions": [SUB_LIFE, SUB_EVICT]})


class RoutedSearch(FakeSearch):
    """Hits chosen by the exact sub-question; any other query (a follow-up) gets the extra page."""

    def __init__(self, *, on_call: Callable[[], None] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.on_call = on_call

    async def search(self, query: SearchQuery):
        self.calls.append(query.text)
        if self.on_call is not None:
            self.on_call()
        if self.gate is not None:
            await self.gate.wait()
        if query.text in HITS:
            return await self._as_results(query, HITS[query.text])
        return await self._as_results(query, EXTRA_HITS)

    @staticmethod
    def _as_results(query: SearchQuery, found: Sequence[dict]):
        from backend.research.mock_search import MockSearchProvider

        return MockSearchProvider({query.text: list(found)}).search(query)


class DeepLLM(ScriptedLLM):
    """Answers the plan request with ``plan``; everything else is claim extraction."""

    def __init__(self, claims, plan: str | Exception | None = PLAN, on_extract=None, **kw) -> None:
        super().__init__(claims, **kw)
        self.plan = plan
        self.plans = 0
        self.on_extract = on_extract
        self.on_plan: Callable[[], None] | None = None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.messages[0].content.startswith("You split a research question"):
            self.plans += 1
            if self.on_plan is not None:
                self.on_plan()
            if isinstance(self.plan, Exception):
                raise self.plan
            return CompletionResponse(text=self.plan or "", provider="fake", model="plan")
        answer = await super().complete(request)
        if self.on_extract is not None:
            self.on_extract()
        return answer


class Env:
    def __init__(self, tmp_path: Path, *, search=None, llm=None, pages=None) -> None:
        self.search = search or RoutedSearch()
        reader, self.transport = build_reader(PAGES if pages is None else pages)
        self.llm = llm or DeepLLM(CLAIMS)
        self.client = TestClient(
            create_app(
                Settings(db_path=tmp_path / "deep.sqlite3", research_enabled=True),
                self.llm,
                search_provider=self.search,
                page_reader=reader,
            )
        )

    def __enter__(self) -> "Env":
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    def start(self, level: str = "deep"):
        return self.client.post(
            "/api/research/sessions", json={"question": QUESTION, "level": level}, headers=ORIGIN
        )

    def wait(self, session_id: str, wanted: str = "completed") -> dict:
        deadline = time.monotonic() + 15
        detail: dict = {}
        while time.monotonic() < deadline:
            detail = self.client.get(f"/api/research/sessions/{session_id}").json()
            if detail["status"] == wanted:
                return detail
            time.sleep(0.02)
        raise AssertionError(f"never {wanted}: {detail.get('status')}")

    def run(self, wanted: str = "completed") -> dict:
        response = self.start()
        assert response.status_code == 202
        return self.wait(response.json()["id"], wanted)


# ----- the plan -----


def test_validate_plan_accepts_a_strict_plan_and_fences() -> None:
    assert validate_plan(PLAN) == [SUB_LIFE, SUB_EVICT]
    assert validate_plan(f"```json\n{PLAN}\n```") == [SUB_LIFE, SUB_EVICT]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({"sub_questions": ["only one question"]}),
        json.dumps({"sub_questions": [f"question number {i}" for i in range(6)]}),
        json.dumps({"sub_questions": ["same thing", "SAME thing"]}),
        json.dumps({"sub_questions": ["see http://evil.test now", "another one"]}),
        json.dumps({"sub_questions": ["ab", "a fine question"]}),
        json.dumps({"sub_questions": ["x" * 201, "a fine question"]}),
        json.dumps({"sub_questions": ["a fine question", 5]}),
        json.dumps({"sub_questions": ["a fine question", "another fine one"], "extra": 1}),
        None,
        "x" * 5000,
    ],
)
def test_validate_plan_rejects_everything_else(raw: object) -> None:
    assert validate_plan(raw) is None


def test_validate_plan_cleans_control_characters() -> None:
    raw = json.dumps({"sub_questions": ["first\x00 topic here", "second\ntopic here"]})
    assert validate_plan(raw) == ["first topic here", "second topic here"]


def test_fallback_plan_splits_comparison_targets_else_keeps_the_question() -> None:
    split = fallback_plan("PostgreSQL vs MySQL for logging")
    assert len(split) >= 2 and "PostgreSQL" in split[0] and "MySQL" in split[1]
    assert fallback_plan(QUESTION) == [QUESTION]


@pytest.mark.parametrize(
    "plan", [RuntimeError("vendor down"), "garbage", json.dumps({"sub_questions": ["one"]})]
)
def test_a_bad_plan_falls_back(plan) -> None:
    llm = DeepLLM(CLAIMS, plan=plan)
    subs, source = asyncio.run(plan_sub_questions(llm, QUESTION))
    assert source is PlanSource.FALLBACK and subs == [QUESTION]


def test_a_good_plan_is_used() -> None:
    subs, source = asyncio.run(plan_sub_questions(DeepLLM(CLAIMS), QUESTION))
    assert source is PlanSource.MODEL and subs == [SUB_LIFE, SUB_EVICT]


# ----- end to end through the HTTP API -----


def test_a_deep_run_researches_every_sub_question_and_groups_the_claims(tmp_path: Path) -> None:
    with Env(tmp_path) as env:
        detail = env.run()
        tasks = env.client.get("/api/tasks").json()
    assert detail["level"] == "deep" and detail["failure_reason"] is None
    text = detail["result_text"]
    lines = text.splitlines()
    first = lines.index(f"Sub-question 1: {SUB_LIFE}")
    second = lines.index(f"Sub-question 2: {SUB_EVICT}")
    assert first < second
    assert any(LIFE_A in line for line in lines[first:second])
    assert any(EVICT_C in line for line in lines[second:])
    assert not any(EVICT_C in line for line in lines[first:second])
    # One numbering: every "[n]" cited in a claim line is listed under Sources with the same n.
    sources_at = lines.index("Sources:")
    for line in lines[first:sources_at]:
        if line.startswith("- ") and line.endswith("]"):
            number = line.rsplit("[", 1)[1]
            assert any(entry.startswith(f"[{number}") for entry in lines[sources_at:])
    assert {c["claim_text"] for c in detail["claims"]} >= {LIFE_A, EVICT_C}
    assert env.llm.plans == 1
    items = tasks["tasks"] if isinstance(tasks, dict) else tasks
    mine = [t for t in items if detail["id"] in t["goal"]]
    assert len(mine) == 1 and mine[0]["status"] == "completed"
    assert mine[0]["steps_total"] == 4 and mine[0]["steps_completed"] == 4


def test_follow_up_rounds_add_a_source_and_stay_bounded(tmp_path: Path) -> None:
    with Env(tmp_path) as env:
        detail = env.run()
    urls = {s["url"] for s in detail["sources"]}
    assert URL_X in urls, "a follow-up round found the extra page"
    assert len(env.search.calls) <= DEEP_MAX_SEARCH_QUERIES
    assert len(env.transport.requests) <= DEEP_MAX_PAGES
    assert len(detail["queries"]) == len(env.search.calls)


def test_only_verified_quotes_and_no_absence_claims_reach_the_result(tmp_path: Path) -> None:
    invented = "The Foo widget cache is stored on a quantum disk."
    claims = {**CLAIMS, URL_LA: [(LIFE_A, LIFE_A), (invented, "stored on a quantum disk")]}
    with Env(tmp_path, llm=DeepLLM(claims)) as env:
        detail = env.run()
    assert invented not in detail["result_text"]
    assert CAVEAT_TEXT[Caveat.CLAIMS_REMOVED] in detail["result_text"]


def test_a_failed_plan_still_researches_with_a_caveat(tmp_path: Path) -> None:
    with Env(tmp_path, llm=DeepLLM(CLAIMS, plan="not json")) as env:
        detail = env.run()
    assert CAVEAT_TEXT[Caveat.PLAN_FALLBACK] in detail["result_text"]
    assert f"Sub-question 1: {QUESTION}" in detail["result_text"]


def test_no_verified_claim_fails_with_a_fixed_code_and_no_result(tmp_path: Path) -> None:
    with Env(tmp_path, llm=DeepLLM({})) as env:
        detail = env.run("failed")
    assert detail["failure_reason"] == "synthesis_failed"
    assert detail["result_text"] is None and detail["claims"] == []


def test_a_failing_search_provider_never_produces_an_answer(tmp_path: Path) -> None:
    search = RoutedSearch(failure=SearchFailure.UNAVAILABLE)

    async def boom(query):
        raise SearchError(SearchFailure.UNAVAILABLE)

    search.search = boom  # type: ignore[method-assign]
    with Env(tmp_path, search=search) as env:
        detail = env.run("failed")
    assert detail["failure_reason"] == "search_failed" and detail["result_text"] is None


def test_the_search_budget_running_out_mid_run_gives_a_partial_result(tmp_path: Path) -> None:
    inner = RoutedSearch()
    guarded = BudgetedSearchProvider(
        inner, monthly_limit=1, usage_counter=lambda: len(inner.calls)
    )
    with Env(tmp_path, search=guarded) as env:
        detail = env.run()
    assert len(inner.calls) == 1, "the budget guard stopped the searches"
    assert LIFE_A in detail["result_text"]
    assert CAVEAT_TEXT[Caveat.SEARCH_BUDGET_EXHAUSTED] in detail["result_text"]
    assert CAVEAT_TEXT[Caveat.SUB_QUESTION_NO_CLAIMS] in detail["result_text"]
    assert detail["claims"], "partial results only exist with a verified claim"


def test_cancel_stops_a_deep_run_and_leaves_no_answer(tmp_path: Path) -> None:
    gate = Gate()
    with Env(tmp_path, search=RoutedSearch(gate=gate)) as env:
        session_id = env.start().json()["id"]
        assert gate.wait_until_entered()
        live = env.client.get(f"/api/research/sessions/{session_id}").json()
        assert live["progress"]["sub_questions"] == 2 and live["progress"]["sub_question"] == 1
        assert live["progress"]["stage"] == "searching"
        response = env.client.post(f"/api/research/sessions/{session_id}/cancel", headers=ORIGIN)
        assert response.json() == {"id": session_id, "status": "cancelling"}
        detail = env.wait(session_id, "cancelled")
        assert detail["result_text"] is None and detail["claims"] == []
        assert env.start().status_code == 202  # free again


def test_levels_are_validated(tmp_path: Path) -> None:
    assert ResearchLevel.DEEP in REQUESTABLE_LEVELS
    assert ResearchLevel.EXTENSIVE not in REQUESTABLE_LEVELS
    with Env(tmp_path) as env:
        for bad in ("extensive", "memory", "Deep", "fast"):
            response = env.start(bad)
            assert (response.status_code, response.json()) == (422, {"detail": "invalid_level"})
        assert env.start("deep").status_code == 202


def test_the_chat_level_setting_accepts_deep() -> None:
    from backend.chat.research_start import RunServiceStarter

    assert Settings(db_path=Path("x.sqlite3"), chat_research_level="deep").chat_research_level
    RunServiceStarter(None, ResearchLevel.DEEP)
    assert Settings(db_path=Path("x.sqlite3")).chat_research_level == "quick"


# ----- budgets, injected clock (library level) -----


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _deep(tmp_path: Path, clock: Clock, *, llm: DeepLLM, search=None, limits=None):
    h = Harness(tmp_path, search=search or RoutedSearch(), pages=PAGES, llm=llm)
    research = DeepResearch(h.search, h.reader, h.repository, llm, limits, clock=clock)
    return h, research


def test_the_time_limit_returns_a_partial_result_with_a_caveat(tmp_path: Path) -> None:
    clock = Clock()
    llm = DeepLLM(CLAIMS)

    def jump() -> None:
        clock.now = 700.0  # the first sub-question took longer than the 600 s limit

    llm.on_extract = jump
    h, research = _deep(tmp_path, clock, llm=llm)
    result = asyncio.run(research.run(QUESTION))
    assert result.session.status is ResearchStatus.COMPLETED
    assert CAVEAT_TEXT[Caveat.TIME_BUDGET_EXHAUSTED] in result.session.result_text
    assert result.claims and len(h.search.calls) < 3 + 3 + 2


def test_the_time_limit_without_a_verified_claim_fails_as_timeout(tmp_path: Path) -> None:
    clock = Clock()
    llm = DeepLLM(CLAIMS)
    llm.on_plan = lambda: setattr(clock, "now", 10_000.0)
    h, research = _deep(tmp_path, clock, llm=llm)
    result = asyncio.run(research.run(QUESTION))
    assert result.session.status is ResearchStatus.FAILED
    assert result.session.failure_reason is FailureReason.TIMEOUT
    assert h.search.calls == [] and result.session.result_text is None


def test_progress_reports_the_stage_per_sub_question(tmp_path: Path) -> None:
    clock = Clock()
    h, research = _deep(tmp_path, clock, llm=DeepLLM(CLAIMS))
    events = []
    asyncio.run(research.run(QUESTION, on_progress=events.append))
    assert {e.sub_question for e in events} >= {1, 2}
    assert all(e.sub_questions in (0, 2) for e in events)
    assert events[0].stage.value == "planning" and events[-1].stage.value == "writing"
    assert any(e.round_index > 0 for e in events), "a follow-up round was reported"


def test_hard_budgets_hold_with_many_hits_and_five_sub_questions(tmp_path: Path) -> None:
    pages = {
        f"https://site{i}.test/foo-{i}": foo_page(f"Foo {i}", f"Foo fact {i}.") for i in range(60)
    }
    hits = [hit(url, f"Foo widget cache page {i}") for i, url in enumerate(pages)]

    class Many(FakeSearch):
        async def search(self, query):
            self.calls.append(query.text)
            from backend.research.mock_search import MockSearchProvider

            return await MockSearchProvider({query.text: hits[: query.max_results]}).search(query)

    plan = json.dumps({"sub_questions": [f"Foo widget cache topic number {i}" for i in range(5)]})
    claims = {url: [(f"Foo fact {i}.", f"Foo fact {i}.")] for i, url in enumerate(pages)}
    llm = DeepLLM(claims, plan=plan)
    h = Harness(tmp_path, search=Many(hits), pages=pages, llm=llm)
    research = DeepResearch(h.search, h.reader, h.repository, llm, clock=Clock())
    result = asyncio.run(research.run(QUESTION))
    assert len(h.search.calls) <= DEEP_MAX_SEARCH_QUERIES
    assert len(h.transport.requests) <= DEEP_MAX_PAGES
    assert len(result.claims) <= deep.DEEP_MAX_TOTAL_CLAIMS
    assert result.search_rounds <= deep.DEEP_MAX_FOLLOW_UP_ROUNDS
    assert len(result.sub_questions) <= deep.DEEP_MAX_SUB_QUESTIONS


def test_limits_cannot_exceed_the_constants() -> None:
    for name, value in (
        ("max_sub_questions", 6),
        ("max_queries", DEEP_MAX_SEARCH_QUERIES + 1),
        ("max_pages", DEEP_MAX_PAGES + 1),
        ("time_limit", 601.0),
        ("max_total_claims", 31),
    ):
        with pytest.raises(ValueError):
            DeepLimits(**{name: value})
    assert DeepLimits(max_queries=4).max_queries == 4


# ----- reuse and level order -----


def test_a_deep_result_satisfies_lower_levels_but_not_the_other_way(tmp_path: Path) -> None:
    h = Harness(tmp_path, search=RoutedSearch(), pages=PAGES, llm=DeepLLM(CLAIMS))
    research = DeepResearch(h.search, h.reader, h.repository, h.llm, clock=Clock())
    deep_done = asyncio.run(research.run(QUESTION))
    assert deep_done.session.status is ResearchStatus.COMPLETED
    for level in (ResearchLevel.QUICK, ResearchLevel.STANDARD, ResearchLevel.DEEP):
        decision = decide_reuse(h.repository, QUESTION, level, NOW)
        assert decision.reason is ReuseReason.REUSED_FRESH, level
    (tmp_path / "std").mkdir()
    h3 = Harness(tmp_path / "std", search=RoutedSearch(), pages=PAGES, llm=DeepLLM(CLAIMS))
    sid = h3.service.submit(QUESTION, ResearchLevel.STANDARD)

    async def go() -> None:
        await h3.service.run_one()

    asyncio.run(go())
    assert h3.repository.get_session(sid).status is ResearchStatus.COMPLETED
    assert (
        decide_reuse(h3.repository, QUESTION, ResearchLevel.DEEP, NOW).reason
        is ReuseReason.NO_PRIOR_RESEARCH
    )
