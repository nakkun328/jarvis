"""Tavily web search behind the vendor-neutral ``SearchProvider`` contract.

What is sent: the query text, ``search_depth`` ``basic`` (an advanced search costs double),
a bounded ``max_results`` and an optional ``time_range``. The request never asks for the
vendor's generated answer, raw page content or images, and the response is read through an
allow-list: only ``title``, ``url``, ``content`` (as the snippet) and ``published_date``
are copied out. Scores, answers, page text and everything else are ignored, because a
vendor summary or page text is never evidence; page text comes only from the Reader.

Results are untrusted data and pass through ``normalize_results``. Failures raise
``SearchError`` with a fixed reason; vendor error text, the credential and the request body
are never carried, logged or chained. Logs hold fixed event names and a status class only.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Final, NoReturn

import httpx

from backend.core.config import ConfigError
from backend.research.normalizer import (
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    FieldMap,
    NormalizationReport,
    normalize_results,
)
from backend.research.search import (
    MAX_URL_CHARS,
    SearchError,
    SearchFailure,
    SearchQuery,
    SearchResult,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME: Final = "tavily"
ENDPOINT: Final = "https://api.tavily.com/search"

DEFAULT_MAX_RESULTS_CAP: Final = 10
MAX_QUERY_CHARS: Final = 400
REQUEST_TIMEOUT_SECONDS: Final = 10.0
TOTAL_DEADLINE_SECONDS: Final = 25.0
RETRY_BACKOFF_SECONDS: Final = 0.5
MAX_ATTEMPTS: Final = 2  # one request plus at most one retry
MAX_RESPONSE_BYTES: Final = 1_000_000
MAX_RAW_ENTRIES: Final = 50

# Only these vendor fields are read. ``rank`` is empty on purpose: the vendor's relevance
# score is not evidence and is ignored; rank follows the order the vendor returned.
FIELD_MAP: Final = FieldMap(
    url=("url",),
    title=("title",),
    snippet=("content",),
    rank=(),
    published=("published_date",),
)

_MIN_CREDENTIAL_CHARS: Final = 8
_MAX_CREDENTIAL_CHARS: Final = 512
_MAX_RAW_TEXT_CHARS: Final = max(MAX_SNIPPET_CHARS, MAX_TITLE_CHARS) * 4
_MAX_CREDITS: Final = 1000
_TIME_RANGE_BUCKETS: Final = ((1, "day"), (7, "week"), (31, "month"), (366, "year"))


class _Credential:
    """Holds the key so that repr, str and vars() output never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def _check_credential(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _MIN_CREDENTIAL_CHARS <= len(value.strip()) <= _MAX_CREDENTIAL_CHARS
    ):
        raise ConfigError("The search credential is missing or malformed")
    value = value.strip()
    if not all(0x21 <= ord(ch) <= 0x7E for ch in value):
        raise ConfigError("The search credential is missing or malformed")
    return value


def time_range_for(recency_days: int | None) -> str | None:
    """Smallest vendor bucket that covers ``recency_days``; ``None`` beyond a year."""
    if recency_days is None:
        return None
    for days, name in _TIME_RANGE_BUCKETS:
        if recency_days <= days:
            return name
    return None


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


class TavilySearchProvider:
    """``SearchProvider`` backed by the Tavily search API.

    ``transport`` injects an ``httpx`` transport (tests use ``httpx.MockTransport``);
    ``clock``, ``monotonic`` and ``sleep`` are injectable so retries and the deadline are
    testable without real time. After a successful call ``last_credits`` holds the credit
    usage the vendor reported (``None`` if absent) and ``last_report`` the normalisation
    report.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        credential: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_results_cap: int = DEFAULT_MAX_RESULTS_CAP,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        total_deadline: float = TOTAL_DEADLINE_SECONDS,
        backoff: float = RETRY_BACKOFF_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (
            isinstance(max_results_cap, bool)
            or not isinstance(max_results_cap, int)
            or not 1 <= max_results_cap <= DEFAULT_MAX_RESULTS_CAP
        ):
            raise ValueError(
                f"max_results_cap must be an integer from 1 to {DEFAULT_MAX_RESULTS_CAP}"
            )
        if not 0 < request_timeout <= 60 or not 0 < total_deadline <= 120 or not 0 <= backoff <= 10:
            raise ValueError("timeouts and backoff are outside the allowed range")
        self._credential = _Credential(_check_credential(credential))
        self._transport = transport
        self._max_results_cap = max_results_cap
        self._request_timeout = float(request_timeout)
        self._total_deadline = float(total_deadline)
        self._backoff = float(backoff)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._sleep = sleep
        self.last_credits: int | None = None
        self.last_report: NormalizationReport | None = None

    def __repr__(self) -> str:
        return "TavilySearchProvider()"

    __str__ = __repr__

    async def search(self, query: SearchQuery) -> Sequence[SearchResult]:
        text = " ".join(query.text.split())
        if len(text) > MAX_QUERY_CHARS:
            raise SearchError(SearchFailure.INVALID_QUERY)
        body: dict[str, object] = {
            "query": text,
            "search_depth": "basic",
            "topic": "general",
            "max_results": min(query.max_results, self._max_results_cap),
            "include_answer": False,
            "include_raw_content": False,
        }
        time_range = time_range_for(query.recency_days)
        if time_range is not None:
            body["time_range"] = time_range
        # ``query.language`` is not sent: the vendor's accepted values are unverified.

        self.last_credits = None
        payload = await self._post_with_retry(body)
        raw = payload.get("results")
        if not isinstance(raw, list):
            self._fail(SearchFailure.BAD_RESPONSE, "invalid_shape")
        self.last_credits = _credits(payload.get("usage"))
        entries = [_allowed_fields(item) for item in raw[:MAX_RAW_ENTRIES]]
        results, self.last_report = normalize_results(
            entries,
            provider=self.name,
            retrieved_at=self._clock().astimezone(UTC),
            max_results=min(query.max_results, self._max_results_cap),
            field_map=FIELD_MAP,
        )
        logger.info("search_ok", extra={"provider": self.name, "results": len(results)})
        return results

    # ----- transport -----

    async def _post_with_retry(self, body: dict[str, object]) -> dict[str, object]:
        deadline = self._monotonic() + self._total_deadline
        attempt = 0
        while True:
            attempt += 1
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                self._fail(SearchFailure.TIMEOUT, "deadline")
            try:
                return await self._post_once(body, min(self._request_timeout, remaining))
            except SearchError as exc:
                retryable = exc.reason in {SearchFailure.TIMEOUT, SearchFailure.UNAVAILABLE}
                if not retryable or attempt >= MAX_ATTEMPTS:
                    raise
                if deadline - self._monotonic() <= self._backoff:
                    raise
            logger.info("search_retry", extra={"provider": self.name})
            await self._sleep(self._backoff)

    async def _post_once(self, body: dict[str, object], timeout: float) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self._credential.reveal()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(timeout),
            ) as client:
                return await asyncio.wait_for(self._exchange(client, headers, body), timeout)
        except SearchError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            self._fail(SearchFailure.TIMEOUT, "timeout")
        except (httpx.HTTPError, OSError, ValueError):
            # Messages from these may echo the request, so only the class is recorded.
            self._fail(SearchFailure.NETWORK_ERROR, "network")

    async def _exchange(
        self, client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, object]
    ) -> dict[str, object]:
        request = client.build_request("POST", ENDPOINT, headers=headers, json=body)
        response = await client.send(request, stream=True)
        try:
            status = response.status_code
            if status != 200:
                self._fail(_failure_for_status(status), _status_class(status))
            declared = response.headers.get("content-length", "")
            if declared.isdecimal() and int(declared) > MAX_RESPONSE_BYTES:
                self._fail(SearchFailure.BAD_RESPONSE, "too_large")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    self._fail(SearchFailure.BAD_RESPONSE, "too_large")
        finally:
            await response.aclose()
        try:
            payload = json.loads(bytes(data))
        except (ValueError, RecursionError):
            self._fail(SearchFailure.BAD_RESPONSE, "invalid_json")
        if not isinstance(payload, dict):
            self._fail(SearchFailure.BAD_RESPONSE, "invalid_shape")
        return payload

    def _fail(self, reason: SearchFailure, detail: str) -> NoReturn:
        # Fixed vocabulary only: never the query, the vendor's error text or the credential.
        logger.warning(
            "search_failed",
            extra={"provider": self.name, "reason": reason.value, "detail": detail},
        )
        raise SearchError(reason) from None


def _failure_for_status(status: int) -> SearchFailure:
    if status in (400, 422):
        return SearchFailure.INVALID_QUERY
    if status in (401, 403):
        return SearchFailure.UNAUTHORIZED
    if status == 429:
        return SearchFailure.RATE_LIMITED
    if status in (432, 433):
        return SearchFailure.QUOTA_EXHAUSTED
    if status >= 500:
        return SearchFailure.UNAVAILABLE
    return SearchFailure.BAD_RESPONSE


def _allowed_fields(item: object) -> object:
    """Copy only the fields we use, bounded, so nothing else can flow downstream."""
    if not isinstance(item, dict):
        return item
    entry: dict[str, object] = {}
    for key in ("title", "content", "published_date"):
        value = item.get(key)
        if isinstance(value, str):
            entry[key] = value[:_MAX_RAW_TEXT_CHARS]
    url = item.get("url")
    if isinstance(url, str):
        entry["url"] = url[: MAX_URL_CHARS + 1]
    return entry


def _credits(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get("credits")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_CREDITS:
        return value
    return None
