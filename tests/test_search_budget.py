"""Search configuration, the local monthly budget guard and the provider factory."""

import asyncio
import logging
import socket
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core.config import ConfigError, Settings
from backend.core.database import Database
from backend.research.mock_search import MockSearchProvider
from backend.research.repository import ResearchRepository
from backend.research.search import SearchError, SearchFailure, SearchQuery
from backend.research.search_budget import BudgetedSearchProvider, repository_usage_counter
from backend.research.search_factory import create_search_provider

CREDENTIAL = "tvly-test-credential-0123456789abcdef"
HIT = {"title": "T", "url": "https://example.com/a", "snippet": "s"}


@pytest.fixture(autouse=True)
def forbid_real_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a test attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JARVIS_SEARCH_PROVIDER", "JARVIS_SEARCH_API_KEY", "JARVIS_SEARCH_MONTHLY_LIMIT"):
        monkeypatch.delenv(name, raising=False)


# ----- configuration -----


def test_search_is_off_by_default(tmp_path: Path) -> None:
    settings = Settings.from_env()
    assert (settings.search_provider, settings.search_key, settings.search_monthly_limit) == (
        "none",
        None,
        800,
    )
    assert create_search_provider(settings) is None
    assert Settings(db_path=tmp_path / "x").search_provider == "none"


def test_tavily_requires_a_key_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "tavily")
    with pytest.raises(ConfigError, match="JARVIS_SEARCH_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", "   ")
    with pytest.raises(ConfigError, match="JARVIS_SEARCH_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", f" {CREDENTIAL} ")
    settings = Settings.from_env()
    assert settings.search_provider == "tavily" and settings.search_key == CREDENTIAL


def test_key_is_hidden_from_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "Tavily")
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", CREDENTIAL)
    settings = Settings.from_env()
    assert settings.search_provider == "tavily"
    assert CREDENTIAL not in repr(settings) and CREDENTIAL not in str(settings)


def test_unused_key_with_provider_none_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", CREDENTIAL)
    assert Settings.from_env().search_provider == "none"


@pytest.mark.parametrize("value", ["", "brave", "tavily ai"])
def test_unknown_provider_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", value)
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", CREDENTIAL)
    with pytest.raises(ConfigError, match="JARVIS_SEARCH_PROVIDER"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "-5", "1000001", "lots", "1.5", ""])
def test_monthly_limit_must_be_a_positive_int(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_MONTHLY_LIMIT", value)
    with pytest.raises(ConfigError, match="JARVIS_SEARCH_MONTHLY_LIMIT"):
        Settings.from_env()


def test_monthly_limit_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_MONTHLY_LIMIT", " 250 ")
    assert Settings.from_env().search_monthly_limit == 250


def test_settings_validate_directly(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "x", search_provider="tavily")
    with pytest.raises(ConfigError):
        Settings(db_path=tmp_path / "x", search_monthly_limit=True)  # type: ignore[arg-type]


# ----- wrapper -----


class Counter:
    def __init__(self, used: int = 0) -> None:
        self.used = used
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.used


def search(provider: BudgetedSearchProvider, text: str = "q"):
    return asyncio.run(provider.search(SearchQuery(text)))


def test_allows_below_the_limit_and_refuses_at_it() -> None:
    inner = MockSearchProvider({"q": [HIT]})
    counter = Counter(used=2)
    provider = BudgetedSearchProvider(inner, monthly_limit=3, usage_counter=counter)
    assert len(search(provider)) == 1
    counter.used = 3
    with pytest.raises(SearchError) as info:
        search(provider)
    assert info.value.reason is SearchFailure.QUOTA_EXHAUSTED_LOCAL
    assert info.value.reason.value == "quota_exhausted_local"
    assert len(inner.calls) == 1  # the refused call never reached the provider
    counter.used = 10
    with pytest.raises(SearchError):
        search(provider)
    assert len(inner.calls) == 1


def test_in_flight_searches_count_against_the_limit() -> None:
    seen: list[int] = []

    class Slow:
        name = "slow"

        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.calls = 0

        async def search(self, query: SearchQuery):
            self.calls += 1
            await self.release.wait()
            return []

    async def scenario() -> None:
        inner = Slow()
        provider = BudgetedSearchProvider(inner, monthly_limit=3, usage_counter=Counter(used=1))
        tasks = [asyncio.ensure_future(provider.search(SearchQuery("q"))) for _ in range(4)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        seen.append(provider.in_flight)
        done = [t for t in tasks if t.done()]
        assert len(done) == 2  # 1 recorded + 2 in flight reaches the limit of 3
        for task in done:
            assert task.exception().reason is SearchFailure.QUOTA_EXHAUSTED_LOCAL
        assert inner.calls == 2
        inner.release.set()
        await asyncio.gather(*(t for t in tasks if not t.done()))
        assert provider.in_flight == 0
        await provider.search(SearchQuery("q"))  # capacity is back once they finish

    asyncio.run(scenario())
    assert seen == [2]


def test_in_flight_is_released_on_failure() -> None:
    inner = MockSearchProvider(failures={"q": SearchFailure.UNAVAILABLE})
    provider = BudgetedSearchProvider(inner, monthly_limit=2, usage_counter=Counter())
    for _ in range(3):
        with pytest.raises(SearchError) as info:
            search(provider)
        assert info.value.reason is SearchFailure.UNAVAILABLE
    assert provider.in_flight == 0


def test_broken_counter_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    inner = MockSearchProvider({"q": [HIT]})

    def broken() -> int:
        raise RuntimeError("database says q is private")

    provider = BudgetedSearchProvider(inner, monthly_limit=5, usage_counter=broken)
    with pytest.raises(SearchError) as info:
        search(provider)
    assert info.value.reason is SearchFailure.UNAVAILABLE and info.value.__cause__ is None
    assert "private" not in caplog.text and inner.calls == []
    for bad in (-1, True, "3", None):
        provider = BudgetedSearchProvider(inner, monthly_limit=5, usage_counter=lambda v=bad: v)
        with pytest.raises(SearchError):
            search(provider)
    assert inner.calls == []


def test_wrapper_validation_and_name() -> None:
    inner = MockSearchProvider()
    for bad in (0, -1, True, 1.5, 1_000_001):
        with pytest.raises(ValueError):
            BudgetedSearchProvider(inner, monthly_limit=bad, usage_counter=Counter())  # type: ignore[arg-type]
    provider = BudgetedSearchProvider(inner, monthly_limit=7, usage_counter=Counter())
    assert provider.name == "mock" and provider.monthly_limit == 7


# ----- repository-backed counter -----


@pytest.fixture
def repository(tmp_path: Path) -> ResearchRepository:
    database = Database(tmp_path / "research.sqlite3")
    database.initialize()
    state = {"now": datetime(2026, 10, 15, tzinfo=UTC)}
    repo = ResearchRepository(database, clock=lambda: state["now"])
    repo.state = state  # type: ignore[attr-defined]
    return repo


def record(repo: ResearchRepository, when: datetime, text: str = "q") -> None:
    repo.state["now"] = when  # type: ignore[attr-defined]
    session = repo.create_session("question")
    repo.add_query(session.id, text)


def test_repository_counts_by_utc_calendar_month(repository: ResearchRepository) -> None:
    moments = [
        datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
        datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
    ]
    for moment in moments:
        record(repository, moment)
    month = repository.count_queries_in_month
    assert month(datetime(2026, 10, 8, tzinfo=UTC)) == 3
    assert month(datetime(2026, 9, 1, tzinfo=UTC)) == 1
    assert month(datetime(2026, 11, 30, tzinfo=UTC)) == 1
    assert month(datetime(2026, 12, 1, tzinfo=UTC)) == 1  # December wraps to January correctly
    assert month(datetime(2027, 1, 15, tzinfo=UTC)) == 1
    assert month(datetime(2026, 8, 1, tzinfo=UTC)) == 0
    # A non-UTC moment is converted: 2026-11-01 08:00 JST is still 31 October in UTC.
    jst = timezone(timedelta(hours=9))
    assert month(datetime(2026, 11, 1, 8, 0, tzinfo=jst)) == 3
    with pytest.raises(ValueError):
        month(datetime(2026, 10, 8))


def test_counter_drives_the_wrapper_across_a_month_boundary(
    repository: ResearchRepository,
) -> None:
    for day in (1, 2):
        record(repository, datetime(2026, 10, day, tzinfo=UTC))
    clock = {"now": datetime(2026, 10, 31, 23, 59, tzinfo=UTC)}
    counter = repository_usage_counter(repository, lambda: clock["now"])
    inner = MockSearchProvider({"q": [HIT]})
    provider = BudgetedSearchProvider(inner, monthly_limit=2, usage_counter=counter)
    with pytest.raises(SearchError) as info:
        search(provider)
    assert info.value.reason is SearchFailure.QUOTA_EXHAUSTED_LOCAL
    clock["now"] = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)  # a new month starts at zero
    assert len(search(provider)) == 1


def test_counting_does_not_write(repository: ResearchRepository) -> None:
    record(repository, datetime(2026, 10, 2, tzinfo=UTC))
    before = repository.list_sessions()
    repository.count_queries_in_month(datetime(2026, 10, 8, tzinfo=UTC))
    assert repository.list_sessions() == before


# ----- factory -----


def test_factory_builds_a_guarded_provider_without_network(
    monkeypatch: pytest.MonkeyPatch, repository: ResearchRepository
) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", CREDENTIAL)
    monkeypatch.setenv("JARVIS_SEARCH_MONTHLY_LIMIT", "2")
    settings = Settings.from_env()
    provider = create_search_provider(settings, repository=repository)
    assert isinstance(provider, BudgetedSearchProvider)
    assert provider.name == "tavily" and provider.monthly_limit == 2
    assert CREDENTIAL not in repr(provider) + repr(vars(provider)) + repr(vars(provider._inner))
    for day in (1, 2):
        record(repository, datetime.now(UTC).replace(day=day, hour=0, minute=1))
    # Over budget: refused locally, so the real transport (and the socket guard) is never hit.
    with pytest.raises(SearchError) as info:
        asyncio.run(provider.search(SearchQuery("q")))
    assert info.value.reason is SearchFailure.QUOTA_EXHAUSTED_LOCAL


def test_factory_fails_closed_without_a_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("JARVIS_SEARCH_API_KEY", CREDENTIAL)
    with pytest.raises(ConfigError) as info:
        create_search_provider(Settings.from_env())
    assert CREDENTIAL not in str(info.value)
    provider = create_search_provider(Settings.from_env(), usage_counter=lambda: 0)
    assert isinstance(provider, BudgetedSearchProvider)


def test_factory_rejects_an_unsupported_name(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "x")
    object.__setattr__(settings, "search_provider", "other")
    with pytest.raises(ConfigError):
        create_search_provider(settings, usage_counter=lambda: 0)
