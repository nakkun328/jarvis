import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.research.mock_search import FIXED_RETRIEVED_AT, MockSearchProvider
from backend.research.search import (
    SearchError,
    SearchFailure,
    SearchProvider,
    SearchQuery,
    SearchResult,
    SourceType,
    query_digest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "research-search-raw-v1.json"
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def make_result(**overrides: object) -> SearchResult:
    fields: dict[str, object] = {
        "title": "Title",
        "url": "https://example.com/a",
        "snippet": "text",
        "rank": 1,
        "provider": "p",
        "retrieved_at": NOW,
    }
    fields.update(overrides)
    return SearchResult(**fields)  # type: ignore[arg-type]


def test_source_type_values_match_shared_contract() -> None:
    assert [t.value for t in SourceType] == [
        "official",
        "docs",
        "academic",
        "news",
        "community",
        "blog",
        "forum",
        "unknown",
    ]


def test_query_defaults_and_bounds() -> None:
    query = SearchQuery("python 3.14 release")
    assert (query.max_results, query.language, query.recency_days) == (5, None, None)
    assert SearchQuery("x" * 500, max_results=20, language="ja", recency_days=1)
    assert SearchQuery("q", language="en-US")
    for kwargs in (
        {"text": ""},
        {"text": "   \n"},
        {"text": "x" * 501},
        {"text": "a\x00b"},
        {"text": "q", "max_results": 0},
        {"text": "q", "max_results": 21},
        {"text": "q", "max_results": True},
        {"text": "q", "max_results": 1.5},
        {"text": "q", "language": ""},
        {"text": "q", "language": "english language"},
        {"text": "q", "recency_days": 0},
        {"text": "q", "recency_days": 3651},
        {"text": "q", "recency_days": True},
        {"text": 5},
    ):
        with pytest.raises(ValueError):
            SearchQuery(**kwargs)  # type: ignore[arg-type]


def test_query_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        SearchQuery("q").text = "other"  # type: ignore[misc]


def test_result_requires_valid_fields() -> None:
    result = make_result()
    assert result.published_at is None
    assert result.source_type is SourceType.UNKNOWN
    assert make_result(published_at=NOW - timedelta(days=3)).published_at is not None
    for overrides in (
        {"title": ""},
        {"snippet": None},
        {"rank": 0},
        {"rank": True},
        {"provider": ""},
        {"url": "ftp://example.com/a"},
        {"url": "/relative"},
        {"url": "https://example.com/a b"},
        {"url": "https://user:x@example.com/"},
        {"url": "https://example.com/" + "a" * 2100},
        {"retrieved_at": datetime(2026, 1, 1)},
        {"retrieved_at": datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))},
        {"published_at": datetime(2026, 1, 1)},
        {"source_type": "mystery"},
    ):
        with pytest.raises(ValueError):
            make_result(**overrides)
    with pytest.raises(FrozenInstanceError):
        result.rank = 2  # type: ignore[misc]
    assert replace(result, rank=2).rank == 2


def test_search_error_carries_only_a_fixed_reason() -> None:
    error = SearchError(SearchFailure.RATE_LIMITED)
    assert error.reason is SearchFailure.RATE_LIMITED
    assert str(error) == "search failed: rate_limited"
    assert SearchError("timeout").reason is SearchFailure.TIMEOUT
    with pytest.raises(ValueError):
        SearchError("Upstream said: key abc is invalid")
    with pytest.raises(TypeError):
        SearchError(SearchFailure.TIMEOUT, "extra upstream text")  # type: ignore[call-arg]


def test_query_digest_never_contains_query_text() -> None:
    digest = query_digest("secret medical question")
    assert "secret" not in digest and digest == query_digest("secret medical question")
    assert digest != query_digest("other")


HITS = [
    {"url": "https://example.com/one", "title": "One", "snippet": "first"},
    {"url": "https://example.com/two", "title": "Two", "snippet": "second"},
    {"url": "https://example.com/three", "title": "Three", "snippet": "third"},
]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_mock_provider_satisfies_protocol_and_returns_ranked_results() -> None:
    provider: SearchProvider = MockSearchProvider({"Python  release": HITS})
    results = run(provider.search(SearchQuery("python release", max_results=2)))
    assert [r.title for r in results] == ["One", "Two"]
    assert [r.rank for r in results] == [1, 2]
    assert {r.provider for r in results} == {"mock"}
    assert {r.retrieved_at for r in results} == {FIXED_RETRIEVED_AT}
    assert isinstance(provider, MockSearchProvider)
    assert provider.last_report is not None and provider.last_report.dropped_count == 1
    assert provider.calls[0].max_results == 2


def test_mock_provider_is_deterministic() -> None:
    provider = MockSearchProvider({"q": HITS})
    first = run(provider.search(SearchQuery("q")))
    second = run(provider.search(SearchQuery("q")))
    assert first == second
    assert len(provider.calls) == 2


def test_mock_explicit_empty_and_unlisted_queries_return_no_results() -> None:
    provider = MockSearchProvider({"nothing here": []})
    assert run(provider.search(SearchQuery("nothing here"))) == ()
    assert run(provider.search(SearchQuery("never configured"))) == ()
    assert provider.last_report is not None and provider.last_report.received_count == 0


def test_mock_injected_failure_raises_search_error_with_reason_only() -> None:
    provider = MockSearchProvider({"q": HITS}, failures={"boom": "unavailable"})
    with pytest.raises(SearchError) as excinfo:
        run(provider.search(SearchQuery("BOOM")))
    assert excinfo.value.reason is SearchFailure.UNAVAILABLE
    assert "boom" not in str(excinfo.value).lower()
    assert len(run(provider.search(SearchQuery("q")))) == 3
    with pytest.raises(ValueError):
        MockSearchProvider(failures={"q": "free text from upstream"})


def test_mock_drops_invalid_canned_hits_and_reports_them() -> None:
    provider = MockSearchProvider({"q": [{"url": "javascript:alert(1)", "title": "x"}, *HITS[:1]]})
    results = run(provider.search(SearchQuery("q")))
    assert [r.url for r in results] == ["https://example.com/one"]
    assert provider.last_report is not None
    assert dict(provider.last_report.reasons) == {"unsupported_scheme": 1}


def test_mock_from_mapping_loads_json_style_data() -> None:
    provider = MockSearchProvider.from_mapping(
        {"responses": {"a": HITS[:1]}, "failures": {"b": "timeout"}}
    )
    assert len(run(provider.search(SearchQuery("a")))) == 1
    with pytest.raises(SearchError):
        run(provider.search(SearchQuery("b")))
    with pytest.raises(ValueError):
        MockSearchProvider.from_mapping({"responses": []})


def test_mock_does_no_io_and_has_no_latency() -> None:
    # The coroutine completes without ever yielding to the event loop.
    coro = MockSearchProvider({"q": HITS}).search(SearchQuery("q"))
    with pytest.raises(StopIteration) as stop:
        coro.send(None)
    assert len(stop.value.value) == 3


def test_fixture_shapes_are_well_formed_json() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == "research-search-raw-v1"
    assert len(data["providers"]) == 3
