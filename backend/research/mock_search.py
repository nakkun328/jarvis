"""Deterministic, fixture-driven search provider for tests and offline development.

No network, no clock, no sleeping. Canned hits are raw provider-style mappings that go
through the real normaliser, so the mock exercises the same pipeline as a live adapter.
"""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Self

from backend.research.normalizer import (
    DEFAULT_FIELD_MAP,
    FieldMap,
    NormalizationReport,
    normalize_results,
)
from backend.research.search import SearchError, SearchFailure, SearchQuery, SearchResult

MOCK_PROVIDER_NAME = "mock"
FIXED_RETRIEVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_WHITESPACE = re.compile(r"\s+")


def query_key(text: str) -> str:
    """Matching key for canned responses: case-folded with whitespace collapsed."""
    return _WHITESPACE.sub(" ", text).strip().casefold()


class MockSearchProvider:
    """Serve canned results per query text.

    ``responses`` maps a query text to raw hit mappings; an empty sequence is an explicit
    zero-hit result and an unlisted query also returns no results. ``failures`` maps a
    query text to the ``SearchFailure`` to raise. ``language`` and ``recency_days`` are
    recorded in ``calls`` but do not change the output.
    """

    name = MOCK_PROVIDER_NAME

    def __init__(
        self,
        responses: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        *,
        failures: Mapping[str, SearchFailure | str] | None = None,
        retrieved_at: datetime = FIXED_RETRIEVED_AT,
        field_map: FieldMap = DEFAULT_FIELD_MAP,
    ) -> None:
        self._responses = {query_key(k): tuple(v) for k, v in (responses or {}).items()}
        self._failures = {query_key(k): SearchFailure(v) for k, v in (failures or {}).items()}
        self._retrieved_at = retrieved_at
        self._field_map = field_map
        self.calls: list[SearchQuery] = []
        self.last_report: NormalizationReport | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Self:
        """Build from JSON-style ``{"responses": {...}, "failures": {...}}`` data."""
        responses = data.get("responses", {})
        failures = data.get("failures", {})
        if not isinstance(responses, Mapping) or not isinstance(failures, Mapping):
            raise ValueError("responses and failures must be objects")
        return cls(responses, failures=failures)

    async def search(self, query: SearchQuery) -> Sequence[SearchResult]:
        self.calls.append(query)
        key = query_key(query.text)
        if key in self._failures:
            raise SearchError(self._failures[key])
        results, self.last_report = normalize_results(
            list(self._responses.get(key, ())),
            provider=self.name,
            retrieved_at=self._retrieved_at,
            max_results=query.max_results,
            field_map=self._field_map,
        )
        return results
