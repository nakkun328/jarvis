"""Local monthly budget guard for a paid search provider.

``BudgetedSearchProvider`` refuses a search, without any network call, once the queries
JARVIS recorded this UTC month plus the searches currently in flight reach the monthly
limit. It counts only what JARVIS itself recorded in ``research_queries``: it is not the
vendor's real credit balance, so it cannot see other clients, trial runs, retries the
vendor billed, or plan changes. It is a conservative local brake, not an accounting system.

If a caller records the query *before* searching, that query is already in the count and
the effective ceiling is one lower than the limit (the safe direction).
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from backend.research.repository import ResearchRepository
from backend.research.search import (
    SearchError,
    SearchFailure,
    SearchProvider,
    SearchQuery,
    SearchResult,
)

logger = logging.getLogger(__name__)

MAX_MONTHLY_LIMIT = 1_000_000


def repository_usage_counter(
    repository: ResearchRepository, clock: Callable[[], datetime] | None = None
) -> Callable[[], int]:
    """Counter of queries recorded in the current UTC calendar month (read-only SQL count)."""
    now = clock or (lambda: datetime.now(UTC))
    return lambda: repository.count_queries_in_month(now())


class BudgetedSearchProvider:
    def __init__(
        self,
        inner: SearchProvider,
        *,
        monthly_limit: int,
        usage_counter: Callable[[], int],
    ) -> None:
        if (
            isinstance(monthly_limit, bool)
            or not isinstance(monthly_limit, int)
            or not 1 <= monthly_limit <= MAX_MONTHLY_LIMIT
        ):
            raise ValueError(f"monthly_limit must be an integer from 1 to {MAX_MONTHLY_LIMIT}")
        self._inner = inner
        self._monthly_limit = monthly_limit
        self._usage_counter = usage_counter
        self._in_flight = 0

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "search")

    @property
    def monthly_limit(self) -> int:
        return self._monthly_limit

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def is_exhausted(self) -> bool:
        """True when the next search would be refused for the local budget (no network).

        A counter that fails or misbehaves reads as "not exhausted" here: this is only an early
        hint for callers that want to refuse before queueing work, and the guard in ``search``
        still fails closed.
        """
        try:
            used = self._usage_counter()
        except Exception:
            return False
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            return False
        return used + self._in_flight >= self._monthly_limit

    async def search(self, query: SearchQuery) -> Sequence[SearchResult]:
        # No await between the check and the increment, so concurrent calls cannot overshoot.
        try:
            used = self._usage_counter()
        except Exception:
            logger.warning("search_budget_check_failed")
            raise SearchError(SearchFailure.UNAVAILABLE) from None
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            logger.warning("search_budget_check_failed")
            raise SearchError(SearchFailure.UNAVAILABLE)
        if used + self._in_flight >= self._monthly_limit:
            logger.warning("search_budget_exhausted")
            raise SearchError(SearchFailure.QUOTA_EXHAUSTED_LOCAL)
        self._in_flight += 1
        try:
            return await self._inner.search(query)
        finally:
            self._in_flight -= 1
