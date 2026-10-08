"""Citation manager: verification, persistence and rendering, independent of any model."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.core.database import Database
from backend.research.citations import (
    MIN_QUOTE_CHARS,
    CitationManager,
    DropReason,
    EvidenceSource,
    ProposedClaim,
    find_quote,
    strip_unknown_urls,
)
from backend.research.models import ResearchStatus
from backend.research.repository import ResearchIntegrityError, ResearchRepository

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
TEXT_1 = "Intro line.\nThe  cache holds\n entries for   sixty seconds.\nOutro."
TEXT_2 = "Second source says: eviction is least recently used."


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def repository(tmp_path: Path) -> ResearchRepository:
    database = Database(tmp_path / "cite.sqlite3")
    database.initialize()
    return ResearchRepository(database, clock=lambda: NOW)


def make_manager(repository: ResearchRepository):
    session = repository.create_session("q")
    repository.transition(session.id, ResearchStatus.PENDING, ResearchStatus.RUNNING)
    published = datetime(2026, 1, 2, tzinfo=UTC)
    first = repository.add_source(
        session.id,
        url="https://a.test/one",
        final_url="https://a.test/one",
        retrieved_at=NOW,
        content_digest=digest(TEXT_1),
        title="Source One",
        published_at=published,
    )
    second = repository.add_source(
        session.id,
        url="https://b.test/two",
        final_url="https://b.test/two",
        retrieved_at=NOW,
        content_digest=digest(TEXT_2),
    )
    evidence = [EvidenceSource(1, first, TEXT_1), EvidenceSource(2, second, TEXT_2)]
    return session, first, second, CitationManager(repository, session.id, evidence)


def test_find_quote_ignores_whitespace_runs_but_not_wording_or_case() -> None:
    quote = "The cache holds entries for sixty seconds."
    start, end = find_quote(TEXT_1, quote)
    assert TEXT_1[start:end] == "The  cache holds\n entries for   sixty seconds."
    assert find_quote(TEXT_1, "the cache holds entries") is None
    assert find_quote(TEXT_1, "The cache holds entries for ninety seconds.") is None
    assert find_quote(TEXT_1, "   ") is None
    assert find_quote(TEXT_1, "sixty seconds. Outro") == (
        TEXT_1.index("sixty"),
        TEXT_1.index("Outro") + len("Outro"),
    )
    assert find_quote("a.b (c)", "a.b (c)") == (0, 7)  # regex metacharacters are literal


def test_verify_keeps_only_quotes_found_in_the_cited_source(repository) -> None:
    _, first, second, manager = make_manager(repository)
    proposed = [
        ProposedClaim("Sixty seconds.", 1, "The cache holds entries for sixty seconds."),
        ProposedClaim("LRU.", 2, "eviction is least recently used"),
        ProposedClaim("Cross-cited.", 2, "The cache holds entries for sixty seconds."),
        ProposedClaim("Unknown.", 3, "eviction is least recently used"),
        ProposedClaim("Zero.", 0, "eviction is least recently used"),
        ProposedClaim("Short.", 2, "x" * (MIN_QUOTE_CHARS - 1)),
        ProposedClaim("Long.", 2, "y" * 501),
        ProposedClaim("  ", 2, "eviction is least recently used"),
        ProposedClaim("x" * 2001, 2, "eviction is least recently used"),
        ProposedClaim("Sixty seconds.", 1, "The cache holds entries for sixty seconds."),
    ]
    report = manager.verify(proposed, max_claims=10)
    assert [(c.text, c.source_index, c.source_id) for c in report.verified] == [
        ("Sixty seconds.", 1, first.id),
        ("LRU.", 2, second.id),
    ]
    assert [d.reason for d in report.dropped] == [
        DropReason.QUOTE_NOT_FOUND,
        DropReason.UNKNOWN_SOURCE,
        DropReason.UNKNOWN_SOURCE,
        DropReason.QUOTE_TOO_SHORT,
        DropReason.QUOTE_TOO_LONG,
        DropReason.MALFORMED,
        DropReason.CLAIM_TOO_LONG,
        DropReason.DUPLICATE,
    ]
    assert report.verified[0].quote == "The cache holds entries for sixty seconds."
    start, end = report.verified[0].quote_start, report.verified[0].quote_end
    assert " ".join(TEXT_1[start:end].split()) == report.verified[0].quote


def test_verify_rejects_non_text_and_non_integer_fields(repository) -> None:
    *_, manager = make_manager(repository)
    bad = [
        ProposedClaim(5, 1, "The cache holds entries for sixty seconds."),  # type: ignore[arg-type]
        ProposedClaim("t", True, "The cache holds entries for sixty seconds."),  # type: ignore[arg-type]
        ProposedClaim("t", 1, None),  # type: ignore[arg-type]
    ]
    report = manager.verify(bad, max_claims=10)
    assert report.verified == ()
    assert [d.reason for d in report.dropped] == [DropReason.MALFORMED] * 3


def test_claim_limit_drops_the_surplus(repository) -> None:
    *_, manager = make_manager(repository)
    proposed = [
        ProposedClaim("A", 1, "The cache holds entries for sixty seconds."),
        ProposedClaim("B", 2, "eviction is least recently used"),
    ]
    report = manager.verify(proposed, max_claims=1)
    assert len(report.verified) == 1
    assert [d.reason for d in report.dropped] == [DropReason.OVER_LIMIT]


def test_persist_stores_verified_claims_once(repository) -> None:
    session, first, _, manager = make_manager(repository)
    report = manager.verify(
        [ProposedClaim("Sixty seconds.", 1, "The cache holds entries for sixty seconds.")],
        max_claims=10,
    )
    stored = manager.persist(report.verified)
    again = manager.persist(report.verified)
    assert stored == again
    assert repository.list_claims(session.id) == list(stored)
    assert stored[0].source_id == first.id
    assert (stored[0].quote_start, stored[0].quote_end) == (
        report.verified[0].quote_start,
        report.verified[0].quote_end,
    )


def test_evidence_from_another_session_cannot_be_persisted(repository) -> None:
    _, first, _, _ = make_manager(repository)
    other = repository.create_session("other")
    manager = CitationManager(repository, other.id, [EvidenceSource(1, first, TEXT_1)])
    report = manager.verify(
        [ProposedClaim("Sixty seconds.", 1, "The cache holds entries for sixty seconds.")],
        max_claims=10,
    )
    with pytest.raises(ResearchIntegrityError):
        manager.persist(report.verified)
    assert repository.list_claims(other.id) == []


def test_render_lists_only_cited_stored_sources_and_removes_unknown_links(repository) -> None:
    *_, manager = make_manager(repository)
    report = manager.verify(
        [ProposedClaim("Sixty seconds.", 1, "The cache holds entries for sixty seconds.")],
        max_claims=10,
    )
    text = manager.render(
        "Entries last sixty seconds (https://a.test/one). Also see https://invented.test/page.",
        report.verified,
        dropped=2,
    )
    assert "(https://a.test/one)" in text
    assert "invented.test" not in text and "[link removed]." in text
    assert "- Sixty seconds. [1]" in text
    assert (
        "[1] Source One - https://a.test/one (retrieved 2026-10-07; published 2026-01-02)" in text
    )
    assert "b.test" not in text
    assert "2 proposed claim(s) were removed" in text


def test_render_without_verified_claims_says_so(repository) -> None:
    *_, manager = make_manager(repository)
    text = manager.render("The sources do not cover this.", ())
    assert text.endswith("No claim could be verified against a source.")
    assert "Sources:" not in text


def test_strip_unknown_urls_handles_punctuation_and_trailing_slash() -> None:
    allowed = {"https://a.test/one"}
    assert strip_unknown_urls("go to https://a.test/one.", allowed) == "go to https://a.test/one."
    assert strip_unknown_urls("go to https://a.test/one/", allowed) == "go to https://a.test/one/"
    assert strip_unknown_urls("(http://x.test/y), ok", allowed) == "([link removed]), ok"
    assert strip_unknown_urls("HTTP://X.TEST/y", allowed) == "[link removed]"
    assert strip_unknown_urls("no links here", allowed) == "no links here"
