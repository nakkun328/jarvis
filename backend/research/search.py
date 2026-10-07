"""Vendor-neutral web search contract for the research engine.

Search results are untrusted data: titles and snippets must never be treated as
instructions, and result URLs are only candidates for the Reader's own policy checks.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

MAX_QUERY_CHARS = 500
MAX_RESULTS_LIMIT = 20
MAX_RECENCY_DAYS = 3650
MAX_URL_CHARS = 2048

_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")


class SourceType(StrEnum):
    OFFICIAL = "official"
    DOCS = "docs"
    ACADEMIC = "academic"
    NEWS = "news"
    COMMUNITY = "community"
    BLOG = "blog"
    FORUM = "forum"
    UNKNOWN = "unknown"


class SearchFailure(StrEnum):
    """Fixed failure reasons; upstream messages are never carried."""

    INVALID_QUERY = "invalid_query"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    BAD_RESPONSE = "bad_response"
    UNAVAILABLE = "unavailable"


class SearchError(RuntimeError):
    """A search provider failed.

    Only the fixed reason is exposed. Adapters should raise it with ``from None`` so that
    upstream exception text (which may echo queries or credentials) is not chained.
    """

    def __init__(self, reason: SearchFailure | str) -> None:
        self.reason = SearchFailure(reason)
        super().__init__(f"search failed: {self.reason.value}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class SearchQuery:
    text: str
    max_results: int = 5
    language: str | None = None
    recency_days: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("query text must be a non-blank string")
        if len(self.text) > MAX_QUERY_CHARS:
            raise ValueError(f"query text must be at most {MAX_QUERY_CHARS} characters")
        if "\x00" in self.text:
            raise ValueError("query text must not contain NUL")
        if not _is_int(self.max_results) or not 1 <= self.max_results <= MAX_RESULTS_LIMIT:
            raise ValueError(f"max_results must be an integer from 1 to {MAX_RESULTS_LIMIT}")
        if self.language is not None and (
            not isinstance(self.language, str) or not _LANGUAGE.fullmatch(self.language)
        ):
            raise ValueError("language must be a language tag such as 'ja' or 'en-US'")
        if self.recency_days is not None and (
            not _is_int(self.recency_days) or not 1 <= self.recency_days <= MAX_RECENCY_DAYS
        ):
            raise ValueError(f"recency_days must be an integer from 1 to {MAX_RECENCY_DAYS}")


def _check_utc(name: str, value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    rank: int
    provider: str
    retrieved_at: datetime
    published_at: datetime | None = None
    source_type: SourceType = SourceType.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("title must be a non-empty string")
        if not isinstance(self.snippet, str):
            raise ValueError("snippet must be a string")
        if not _is_int(self.rank) or self.rank < 1:
            raise ValueError("rank must be a positive integer")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider must be a non-empty string")
        _check_absolute_http_url(self.url)
        _check_utc("retrieved_at", self.retrieved_at)
        if self.published_at is not None:
            _check_utc("published_at", self.published_at)
        SourceType(self.source_type)


def _check_absolute_http_url(url: object) -> None:
    """Cheap structural check; ``research.normalizer.normalize_url`` is the strict path."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ValueError("url must be a non-empty string within the length bound")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("url must not contain whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    if parts.username is not None or "@" in parts.netloc:
        raise ValueError("url must not contain credentials")


class SearchProvider(Protocol):
    """A web search backend.

    Implementations raise ``SearchError`` for every failure and return an empty sequence
    for a genuine zero-hit search; they must not invent results.
    """

    async def search(self, query: SearchQuery) -> Sequence[SearchResult]: ...


def query_digest(text: str) -> str:
    """Length-bounded identifier for logs; never log the query text itself."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"q:{digest}/{min(len(text), MAX_QUERY_CHARS)}"
