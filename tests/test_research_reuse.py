"""Past-research reuse and freshness (JAR-56, JAR-57), with fakes only."""

import asyncio
import functools
from datetime import timedelta
from pathlib import Path

import pytest
from research_run_support import (
    AGREEING_CLAIMS,
    AGREEING_PAGES,
    NOW,
    QUESTION,
    FakeSearch,
    Harness,
    ScriptedLLM,
)

from backend.api.research import session_detail
from backend.research.models import ResearchLevel, ResearchStatus
from backend.research.reuse import (
    DEFAULT_MAX_AGE_DAYS,
    ReuseReason,
    TopicClass,
    classify_topic,
    decide_reuse,
    normalize_question,
    question_similarity,
)

pytestmark = pytest.mark.usefixtures("no_network")


def aio(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


async def run(h: Harness, question: str, level: ResearchLevel = ResearchLevel.STANDARD):
    session_id = h.service.submit(question, level)
    await h.service.run_one()
    return h.repository.get_session(session_id)


# ----- pure helpers -----


def test_normalisation_and_similarity() -> None:
    assert normalize_question("  Ｆｏｏ, Widget?! ") == "foowidget"
    assert question_similarity("Foo widget cache?", "foo  widget CACHE") == 1.0
    assert question_similarity("a", "") == 0.0
    assert question_similarity("Foo widget cache TTL", "weather in Paris") < 0.3
    near = question_similarity("Pythonの型ヒントとは何ですか", "Pythonの型ヒントとは何か")
    assert 0.7 <= near < 1


@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("東京の天気は?", TopicClass.TIME_SENSITIVE),
        ("PS5の最新価格", TopicClass.TIME_SENSITIVE),
        ("What is the latest Python release?", TopicClass.TIME_SENSITIVE),
        ("Bitcoin price", TopicClass.TIME_SENSITIVE),
        ("How does TCP slow start work?", TopicClass.STABLE),
        ("Why is the unknown word known?", TopicClass.STABLE),  # "now" only as a whole word
        ("光合成とは何か", TopicClass.STABLE),
    ],
)
def test_topic_vocabulary(question: str, topic: TopicClass) -> None:
    assert classify_topic(question) is topic


# ----- decisions through the real run -----


@aio
async def test_no_prior_research_searches_and_records_the_code(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    session = await run(h, QUESTION)
    assert session.reuse_reason == ReuseReason.NO_PRIOR_RESEARCH.value
    assert session.reuse_of is None and h.search.calls


@aio
async def test_fresh_stable_prior_is_reused_without_searching(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    first = await run(h, QUESTION)
    searches, pages = len(h.search.calls), len(h.transport.requests)

    later = NOW + timedelta(days=DEFAULT_MAX_AGE_DAYS - 1)
    h.service._clock = lambda: later
    second = await run(h, "how long does the foo widget cache keep entries")
    assert second.status is ResearchStatus.COMPLETED
    assert second.reuse_reason == "reused_fresh" and second.reuse_of == first.id
    assert second.reuse_prior_at == NOW
    assert second.result_text == first.result_text
    assert (len(h.search.calls), len(h.transport.requests)) == (searches, pages)
    assert h.repository.list_queries(second.id) == []

    # Original verified citations and dates are kept, with fresh ids.
    old = {
        (c.claim_text, c.quote, c.quote_start, c.quote_end)
        for c in h.repository.list_claims(first.id)
    }
    new = {
        (c.claim_text, c.quote, c.quote_start, c.quote_end)
        for c in h.repository.list_claims(second.id)
    }
    assert new == old and new
    assert {s.retrieved_at for s in h.repository.list_sources(second.id)} == {NOW}
    assert {s.final_url for s in h.repository.list_sources(second.id)} <= set(AGREEING_PAGES)
    ids_old = {s.id for s in h.repository.list_sources(first.id)}
    assert not ids_old & {s.id for s in h.repository.list_sources(second.id)}


@aio
async def test_stale_prior_triggers_a_new_search_and_links_the_previous(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    first = await run(h, QUESTION)
    searches = len(h.search.calls)
    h.service._clock = lambda: NOW + timedelta(days=DEFAULT_MAX_AGE_DAYS + 1)
    second = await run(h, QUESTION)
    assert second.status is ResearchStatus.COMPLETED
    assert second.reuse_reason == "prior_stale" and second.reuse_of == first.id
    assert second.reuse_prior_at == NOW
    assert len(h.search.calls) > searches  # searched again


@aio
async def test_time_sensitive_topic_always_searches_again(tmp_path: Path) -> None:
    question = "What is the latest Foo widget cache entry lifetime?"
    h = Harness(tmp_path, clock=lambda: NOW)
    first = await run(h, question)
    searches = len(h.search.calls)
    second = await run(h, question)  # same instant: still not reused
    assert second.reuse_reason == "time_sensitive_topic" and second.reuse_of == first.id
    assert len(h.search.calls) > searches


@aio
async def test_failed_unverified_and_cancelled_sessions_are_never_reused(tmp_path: Path) -> None:
    # A completed session whose result is only the "no verified claim" notice.
    h = Harness(tmp_path, llm=ScriptedLLM({}, insufficient=True), clock=lambda: NOW)
    unverified = await run(h, QUESTION)
    assert unverified.status is ResearchStatus.COMPLETED
    assert h.repository.list_claims(unverified.id) == []
    # A failed one.
    failing = Harness(tmp_path / "f", search=FakeSearch(failure="unavailable"), clock=lambda: NOW)  # type: ignore[arg-type]
    failed = await run(failing, QUESTION)
    assert failed.status is ResearchStatus.FAILED

    h.llm.claims = dict(AGREEING_CLAIMS)
    h.llm.insufficient = False
    third = await run(h, QUESTION)
    assert third.reuse_reason == "no_prior_research"
    assert h.repository.list_claims(third.id)  # searched and verified this time

    decision = decide_reuse(failing.repository, QUESTION, ResearchLevel.QUICK, NOW)
    assert decision.reason is ReuseReason.NO_PRIOR_RESEARCH


@aio
async def test_a_lower_level_result_does_not_serve_a_higher_level_request(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    await run(h, QUESTION, ResearchLevel.QUICK)
    standard = await run(h, QUESTION, ResearchLevel.STANDARD)
    assert standard.reuse_reason == "no_prior_research"
    quick = await run(h, QUESTION, ResearchLevel.QUICK)
    assert quick.reuse_reason == "reused_fresh"  # a standard result serves a quick request


@aio
async def test_a_dissimilar_question_is_not_a_candidate(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    await run(h, QUESTION)
    other = await run(h, "How do I configure the Bar gadget logging format?")
    assert other.reuse_reason == "no_prior_research"


@aio
async def test_future_dated_prior_counts_as_stale(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    await run(h, QUESTION)
    decision = decide_reuse(h.repository, QUESTION, ResearchLevel.QUICK, NOW - timedelta(days=1))
    assert decision.reason is ReuseReason.PRIOR_STALE


def test_decide_requires_an_aware_clock(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(ValueError):
        decide_reuse(h.repository, QUESTION, ResearchLevel.QUICK, NOW.replace(tzinfo=None))


@aio
async def test_api_detail_exposes_fixed_code_prior_id_and_date(tmp_path: Path) -> None:
    h = Harness(tmp_path, clock=lambda: NOW)
    first = await run(h, QUESTION)
    second = await run(h, QUESTION)
    detail = session_detail(second, [], [], [])
    assert detail["reuse"] == {
        "reason": "reused_fresh",
        "previous_session_id": str(first.id),
        "prior_at": "2026-10-07T12:00:00.000000Z",
    }
    plain = session_detail(first, [], [], [])
    assert plain["reuse"]["reason"] == "no_prior_research"


def test_storage_rejects_an_unknown_reason_code(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    session = h.repository.create_session(QUESTION, ResearchLevel.QUICK)
    with pytest.raises(ValueError):
        h.repository.set_reuse_decision(session.id, "free text")
