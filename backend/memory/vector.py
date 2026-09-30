"""Vendor-neutral contract for a rebuildable semantic memory index.

The vector index only stores memory IDs and embeddings. SQLite/Obsidian remain
the source of truth for memory content and metadata. Embedding generation and
the concrete index adapter are separate, later tasks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Protocol


def _finite_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("vector values and scores must be finite numbers")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("vector values and scores must be finite numbers") from exc
    if not isfinite(number):
        raise ValueError("vector values and scores must be finite numbers")
    return number


def _embedding(values: Sequence[float]) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("embedding must be a sequence of finite numbers")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ValueError("embedding must be a sequence of finite numbers") from exc
    if not result:
        raise ValueError("embedding must contain finite numbers")
    return tuple(_finite_float(value) for value in result)


def _required(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """An embedding for one canonical memory ID in one embedding space.

    ``space`` identifies the embedding model and version. Records from
    different spaces must never be compared in one search.
    """

    memory_id: str
    space: str
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        _required(self.memory_id, "memory_id")
        _required(self.space, "space")
        object.__setattr__(self, "values", _embedding(self.values))


@dataclass(frozen=True, slots=True)
class VectorQuery:
    space: str
    values: tuple[float, ...]
    limit: int = 10

    def __post_init__(self) -> None:
        _required(self.space, "space")
        object.__setattr__(self, "values", _embedding(self.values))
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError("limit must be a positive integer")


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """A candidate ID. Scores are ordered high to low within one space only."""

    memory_id: str
    score: float

    def __post_init__(self) -> None:
        _required(self.memory_id, "memory_id")
        object.__setattr__(self, "score", _finite_float(self.score))


class VectorIndex(Protocol):
    """Async adapter implemented by a chosen vector engine.

    Upsert replaces each ``(space, memory_id)`` entry; delete removes every
    space for each ID. Search returns at most ``query.limit`` distinct IDs in
    descending score order, restricted to ``query.space``. Scores need only be
    ordered within a space; callers must not treat them as probabilities.
    Adapters must reject dimension mismatches within a space.
    """

    async def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    async def delete(self, memory_ids: Sequence[str]) -> None: ...

    async def search(self, query: VectorQuery) -> Sequence[VectorMatch]: ...
