"""Convert raw provider payload entries into the common ``SearchResult`` form.

Everything handled here is untrusted. The normaliser only reshapes it: text is cleaned and
bounded but never interpreted, and nothing is fetched or resolved. Invalid entries are
dropped and counted in a ``NormalizationReport`` rather than silently discarded.
"""

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import quote, urlsplit

from backend.research.search import MAX_URL_CHARS, SearchResult, SourceType

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 500
_ELLIPSIS = "…"

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
    }
)
# Invisible characters that can disguise text (bidi overrides, zero-width space, BOM).
_STRIPPED_CHARS = frozenset("​‎‏‪‫‬‭‮⁦⁧⁨⁩﻿")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = "/%:@!$&'()*+,;=-._~?"
_PCT = re.compile(r"%[0-9a-fA-F]{2}")
_EPOCH_TEXT = re.compile(r"\d{9,13}(?:\.\d+)?")
_MAX_TIMESTAMP_TEXT = 64
_MIN_YEAR, _MAX_YEAR = 1970, 2100
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class DropReason(StrEnum):
    NOT_A_MAPPING = "not_a_mapping"
    MISSING_URL = "missing_url"
    INVALID_URL = "invalid_url"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    USERINFO_URL = "userinfo_url"
    URL_TOO_LONG = "url_too_long"
    DUPLICATE_URL = "duplicate_url"
    OVER_LIMIT = "over_limit"


class UrlRejected(ValueError):
    def __init__(self, reason: DropReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class FieldMap:
    """Candidate raw keys per field, in priority order. Providers may override any of them."""

    url: tuple[str, ...] = ("url", "link", "href")
    title: tuple[str, ...] = ("title", "name")
    snippet: tuple[str, ...] = ("snippet", "description", "content", "summary", "text")
    rank: tuple[str, ...] = ("rank", "position")
    published: tuple[str, ...] = (
        "published_at",
        "published_date",
        "published",
        "page_age",
        "date",
        "datePublished",
    )


DEFAULT_FIELD_MAP = FieldMap()


@dataclass(frozen=True)
class NormalizationReport:
    """Entries removed during normalisation, by fixed reason code (never entry content)."""

    dropped_count: int = 0
    reasons: Mapping[str, int] = field(default_factory=dict)
    received_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", MappingProxyType(dict(sorted(self.reasons.items()))))


def normalize_url(url: str) -> str:
    """Return the canonical form of an absolute http(s) URL or raise ``UrlRejected``."""
    if not isinstance(url, str):
        raise UrlRejected(DropReason.INVALID_URL)
    url = url.strip()
    if not url:
        raise UrlRejected(DropReason.MISSING_URL)
    if len(url) > MAX_URL_CHARS:
        raise UrlRejected(DropReason.URL_TOO_LONG)
    if any(ch.isspace() or unicodedata.category(ch) in {"Cc", "Cf"} for ch in url):
        raise UrlRejected(DropReason.INVALID_URL)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise UrlRejected(DropReason.INVALID_URL) from None
    if not parts.scheme:
        raise UrlRejected(DropReason.INVALID_URL)
    if parts.scheme not in _DEFAULT_PORTS:
        raise UrlRejected(DropReason.UNSUPPORTED_SCHEME)
    if "@" in parts.netloc:
        raise UrlRejected(DropReason.USERINFO_URL)
    if not host or port == 0:
        raise UrlRejected(DropReason.INVALID_URL)
    host = host.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlRejected(DropReason.INVALID_URL) from None
    if not host:
        raise UrlRejected(DropReason.INVALID_URL)
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[parts.scheme]) else f"{host}:{port}"
    path = _PCT.sub(lambda m: m.group().upper(), quote(parts.path or "/", safe=_PATH_SAFE))
    kept = [
        segment
        for segment in parts.query.split("&")
        if segment and not _is_tracking(segment.split("=", 1)[0])
    ]
    query = _PCT.sub(lambda m: m.group().upper(), quote("&".join(kept), safe=_QUERY_SAFE))
    result = f"{parts.scheme}://{netloc}{path}" + (f"?{query}" if query else "")
    if len(result) > MAX_URL_CHARS:
        raise UrlRejected(DropReason.URL_TOO_LONG)
    return result


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith("utm_") or key in _TRACKING_PARAMS


def clean_text(value: object, max_chars: int) -> str:
    """Strip control/invisible characters, collapse whitespace and bound the length.

    Non-string input is treated as missing. The content is otherwise preserved verbatim:
    text that looks like instructions stays inert data.
    """
    if not isinstance(value, str):
        return ""
    chars = []
    for ch in value[: max_chars * 8]:
        if ch.isspace():
            chars.append(" ")
        elif ch in _STRIPPED_CHARS or unicodedata.category(ch) in {"Cc", "Cs", "Co"}:
            continue
        else:
            chars.append(ch)
    text = " ".join("".join(chars).split())
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + _ELLIPSIS
    return text


def parse_timestamp(value: object, *, not_after: datetime | None = None) -> datetime | None:
    """Parse ISO-8601, epoch seconds/milliseconds or RFC 2822 into aware UTC, else ``None``.

    Naive timestamps are taken as UTC. Relative text ("3 days ago") is not parseable and
    never falls back to the current time. Values outside 1970-2100, or later than
    ``not_after``, are rejected.
    """
    parsed = _parse_timestamp(value)
    if parsed is None or not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
        return None
    if not_after is not None and parsed > not_after:
        return None
    return parsed


def _parse_timestamp(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            return _from_epoch(float(value))
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or len(text) > _MAX_TIMESTAMP_TEXT:
            return None
        if _EPOCH_TEXT.fullmatch(text):
            return _from_epoch(float(text))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError, IndexError):
        return None


def _from_epoch(seconds: float) -> datetime | None:
    if seconds != seconds or seconds < 0:  # NaN or negative
        return None
    if seconds >= 1e11:  # milliseconds
        seconds /= 1000
    return _EPOCH + timedelta(seconds=seconds)


def _first_present(raw: Mapping[str, object], keys: Iterable[str]) -> object:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _effective_rank(raw: Mapping[str, object], keys: Iterable[str], position: int) -> int:
    value = _first_present(raw, keys)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str) and value.strip().isdecimal() and len(value.strip()) <= 9:
        value = int(value.strip())
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return position


def normalize_results(
    raw_results: Sequence[object],
    *,
    provider: str,
    retrieved_at: datetime,
    max_results: int = 20,
    field_map: FieldMap = DEFAULT_FIELD_MAP,
) -> tuple[tuple[SearchResult, ...], NormalizationReport]:
    """Normalise a provider's raw result list.

    Rules (see docs/research-search.md): ``url`` is required and normalised; a missing
    title falls back to the URL host; a missing snippet is empty; a missing or unparseable
    ``published_at`` is ``None``; a missing rank falls back to input position. Entries are
    de-duplicated by normalised URL keeping the best rank, ordered by rank, cut to
    ``max_results`` and renumbered 1..n.
    """
    if not isinstance(raw_results, list | tuple):
        raise TypeError("raw_results must be a list or tuple of provider entries")
    if not isinstance(provider, str) or not provider:
        raise ValueError("provider must be a non-empty string")
    if (
        not isinstance(retrieved_at, datetime)
        or retrieved_at.tzinfo is None
        or retrieved_at.utcoffset() is None
    ):
        raise ValueError("retrieved_at must be timezone-aware")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ValueError("max_results must be a positive integer")
    retrieved_at = retrieved_at.astimezone(UTC)

    drops: dict[str, int] = {}

    def drop(reason: DropReason, count: int = 1) -> None:
        drops[reason.value] = drops.get(reason.value, 0) + count

    # url -> (rank, position, title, snippet, published_at)
    best: dict[str, tuple[int, int, str, str, datetime | None]] = {}
    for position, raw in enumerate(raw_results, start=1):
        if not isinstance(raw, Mapping):
            drop(DropReason.NOT_A_MAPPING)
            continue
        raw_url = _first_present(raw, field_map.url)
        if raw_url is None:
            drop(DropReason.MISSING_URL)
            continue
        try:
            url = normalize_url(raw_url)
        except UrlRejected as exc:
            drop(exc.reason)
            continue
        host = urlsplit(url).hostname or ""
        title = clean_text(_first_present(raw, field_map.title), MAX_TITLE_CHARS) or host
        snippet = clean_text(_first_present(raw, field_map.snippet), MAX_SNIPPET_CHARS)
        published = next(
            (
                moment
                for key in field_map.published
                if (moment := parse_timestamp(raw.get(key), not_after=retrieved_at)) is not None
            ),
            None,
        )
        rank = _effective_rank(raw, field_map.rank, position)
        entry = (rank, position, title, snippet, published)
        current = best.get(url)
        if current is None:
            best[url] = entry
        else:
            drop(DropReason.DUPLICATE_URL)
            if entry[:2] < current[:2]:
                best[url] = entry

    ordered = sorted(best.items(), key=lambda item: item[1][:2])
    if len(ordered) > max_results:
        drop(DropReason.OVER_LIMIT, len(ordered) - max_results)
        ordered = ordered[:max_results]
    results = tuple(
        SearchResult(
            title=title,
            url=url,
            snippet=snippet,
            rank=index,
            provider=provider,
            retrieved_at=retrieved_at,
            published_at=published,
            source_type=SourceType.UNKNOWN,
        )
        for index, (url, (_, _, title, snippet, published)) in enumerate(ordered, start=1)
    )
    report = NormalizationReport(
        dropped_count=sum(drops.values()), reasons=drops, received_count=len(raw_results)
    )
    if report.dropped_count:
        logger.debug(
            "search normalisation dropped %d of %d entries (%s)",
            report.dropped_count,
            report.received_count,
            ",".join(f"{k}={v}" for k, v in report.reasons.items()),
        )
    return results, report
