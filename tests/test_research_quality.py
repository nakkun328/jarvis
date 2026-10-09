"""Research quality fixes: verified-only rendering, domain diversity, near-duplicate claims."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from backend.core.database import Database
from backend.research.citations import (
    CitationManager,
    DropReason,
    EvidenceSource,
    ProposedClaim,
    near_duplicate,
)
from backend.research.crosscheck import ClaimInput, EvidenceText, cross_check
from backend.research.domains import blocked_reason, diversify, registrable_domain, split_blocked
from backend.research.models import ResearchStatus
from backend.research.reader import ReadFailure
from backend.research.repository import ResearchRepository
from backend.research.search import SearchResult

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def result(url: str, rank: int = 1) -> SearchResult:
    return SearchResult("t", url, "s", rank, "fake", NOW)


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://docs.a.test/x", "a.test"),
        ("https://A.Test./x", "a.test"),
        ("https://www.example.co.jp/x", "example.co.jp"),
        ("https://news.bbc.co.uk/x", "bbc.co.uk"),
        ("http://203.0.113.5/x", "203.0.113.5"),
        ("https://localhost/x", "localhost"),
        ("not a url", None),
    ],
)
def test_registrable_domain(url: str, domain: str | None) -> None:
    assert registrable_domain(url) == domain


def test_blocked_hits_are_split_off() -> None:
    hits = [result("http://169.254.169.254/"), result("https://a.test/x"), result("http://[::1]/")]
    ok, blocked = split_blocked(hits)
    assert [h.url for h in ok] == ["https://a.test/x"]
    assert [r for _, r in blocked] == [ReadFailure.BLOCKED_HOST, ReadFailure.BLOCKED_HOST]
    assert blocked_reason("ftp://a.test/x") is ReadFailure.BLOCKED_SCHEME
    assert blocked_reason("https://a.test/x") is None


def test_diversify_puts_one_page_per_domain_first_and_keeps_rank_order() -> None:
    hits = [result(u) for u in ("https://x.a.test/1", "https://y.a.test/2", "https://b.test/3")]
    assert [h.url for h in diversify(hits)] == [
        "https://x.a.test/1",
        "https://b.test/3",
        "https://y.a.test/2",
    ]
    assert diversify([]) == []
    same = [result(f"https://a.test/{n}") for n in range(3)]
    assert [h.url for h in diversify(same)] == [h.url for h in same]  # fewer domains than needed


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            "The Foo widget cache keeps entries for 60 seconds.",
            "The Foo widget cache keeps its entries for 60 seconds.",
            True,
        ),
        (
            "The Foo widget cache keeps entries for 60 seconds.",
            "The Foo widget cache keeps entries for 120 seconds.",
            False,
        ),
        (
            "The Foo widget cache keeps entries for 60 seconds.",
            "The Foo widget cache does not keep entries for 60 seconds.",
            False,
        ),
        ("The cache holds 1,000 entries.", "The cache holds 1000 entries.", True),
        ("The Foo cache is flushed on restart.", "Bar uses a different eviction policy.", False),
        ("キャッシュは60秒間保持されます。", "キャッシュは60秒間保持される。", True),
        ("キャッシュは60秒間保持されます。", "キャッシュは120秒間保持されます。", False),
        ("", "", False),
    ],
)
def test_near_duplicate(first: str, second: str, expected: bool) -> None:
    assert near_duplicate(first, second) is expected
    assert near_duplicate(second, first) is expected


TEXT_1 = "Entries last sixty seconds in the cache. The cache entries last sixty seconds, always."
TEXT_2 = "Entries last sixty seconds in the cache."


@pytest.fixture
def manager(tmp_path: Path):
    database = Database(tmp_path / "q.sqlite3")
    database.initialize()
    repository = ResearchRepository(database, clock=lambda: NOW)
    session = repository.create_session("q")
    repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)

    def source(url: str, text: str):
        return repository.add_source(
            session.id,
            url=url,
            final_url=url,
            retrieved_at=NOW,
            content_digest=hashlib.sha256(text.encode()).hexdigest(),
        )

    one, two = source("https://a.test/one", TEXT_1), source("https://b.test/two", TEXT_2)
    evidence = [EvidenceSource(1, one, TEXT_1), EvidenceSource(2, two, TEXT_2)]
    return CitationManager(repository, session.id, evidence)


def test_render_shows_only_verified_claims_never_model_prose(manager) -> None:
    report = manager.verify(
        [ProposedClaim("Sixty seconds.", 1, "Entries last sixty seconds in the cache.")],
        max_claims=5,
    )
    text = manager.render("Quick research result.", report.verified)
    assert text.startswith("Quick research result.")
    assert "- Sixty seconds. [1]" in text


def test_near_duplicates_of_one_source_are_merged_keeping_the_better_cited(manager) -> None:
    short = "Entries last sixty seconds in the cache."
    long_ = "The cache entries last sixty seconds, always."
    report = manager.verify(
        [
            ProposedClaim("Cache entries last sixty seconds.", 1, short),
            ProposedClaim("The cache entries last sixty seconds.", 1, TEXT_1),
        ],
        max_claims=5,
    )
    assert [c.text for c in report.verified] == ["The cache entries last sixty seconds."]
    assert report.verified[0].quote == TEXT_1  # the longer quote is the better citation
    assert [d.reason for d in report.dropped] == [DropReason.NEAR_DUPLICATE]
    assert long_ in TEXT_1


def test_the_same_fact_from_two_sources_is_kept_as_corroboration(manager) -> None:
    quote = "Entries last sixty seconds in the cache."
    report = manager.verify(
        [
            ProposedClaim("Cache entries last sixty seconds.", 1, quote),
            ProposedClaim("Cache entries last sixty seconds.", 2, quote),
        ],
        max_claims=5,
    )
    assert [c.source_index for c in report.verified] == [1, 2]


A, B, C = uuid4(), uuid4(), uuid4()
FACT = "The Foo widget cache keeps entries for 60 seconds."


def test_pages_of_one_domain_do_not_corroborate_each_other() -> None:
    evidence = [EvidenceText(s, FACT) for s in (A, B)]
    claim = ClaimInput(uuid4(), A, FACT)
    same = {A: "a.test", B: "a.test"}
    report = cross_check([claim], evidence, same)
    assert all(s.rating is None for s in report.sources)
    assert report.claims[0].rating is None
    different = cross_check([claim], evidence, {A: "a.test", B: "b.test"})
    assert all(s.rating == 1.0 for s in different.sources)
    assert different.claims[0].rating == 1.0
    assert cross_check([claim], evidence).claims[0].rating == 1.0  # no domains: as before


def test_a_domain_counts_as_one_speaker_in_a_claim_rating() -> None:
    other = "The Foo widget cache keeps entries for 120 seconds."
    evidence = [EvidenceText(A, FACT), EvidenceText(B, FACT), EvidenceText(C, other)]
    claim = ClaimInput(uuid4(), A, FACT)
    report = cross_check([claim], evidence, {A: "a.test", B: "a.test", C: "c.test"})
    assert report.claims[0].rating == 0.5  # a.test (supports) against c.test (contradicts)
