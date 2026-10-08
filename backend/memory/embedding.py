"""Provider-neutral embedding contract for the derived memory index.

An embedding space fixes a model identity, version, and dimension. Changing any
of these requires a new space and a rebuild from canonical memory content.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from backend.memory.vector import VectorQuery, VectorRecord, _embedding

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    """Exact model/version and output dimension used by one vector index space."""

    name: str
    version: str
    dimension: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise ValueError("embedding name must be a nonempty model identifier")
        if not isinstance(self.version, str) or _VERSION.fullmatch(self.version) is None:
            raise ValueError("embedding version must be a nonempty version identifier")
        if (
            isinstance(self.dimension, bool)
            or not isinstance(self.dimension, int)
            or self.dimension < 1
        ):
            raise ValueError("embedding dimension must be a positive integer")

    @property
    def identifier(self) -> str:
        """Stable identifier passed to VectorRecord and VectorQuery."""

        return f"{self.name}@{self.version}:d{self.dimension}"

    def validate(self, values: Sequence[float]) -> tuple[float, ...]:
        """Normalize finite values and reject another space's dimension."""

        vector = _embedding(values)
        if len(vector) != self.dimension:
            raise ValueError("embedding dimension does not match this space")
        return vector

    def record(
        self, memory_id: str, values: Sequence[float], *, source_revision: str | None = None
    ) -> VectorRecord:
        """Build an index record only after checking the configured dimension."""

        return VectorRecord(memory_id, self.identifier, self.validate(values), source_revision)

    def query(self, values: Sequence[float], *, limit: int = 10) -> VectorQuery:
        """Build a query in the same versioned space as its record vectors."""

        return VectorQuery(self.identifier, self.validate(values), limit)


class EmbeddingProvider(Protocol):
    """Async text encoder; concrete implementations own model setup and secrets."""

    space: EmbeddingSpace

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


async def embed_texts(
    provider: EmbeddingProvider, texts: Sequence[str], *, query: bool = False
) -> tuple[tuple[float, ...], ...]:
    """Validate provider output before it reaches a derived vector index.

    The number and order of vectors must match the supplied texts. Empty input
    needs no provider call. Callers supply current canonical content themselves.
    """

    if not isinstance(provider.space, EmbeddingSpace):
        raise ValueError("provider must declare an embedding space")
    if isinstance(texts, (str, bytes)):
        raise ValueError("texts must be a sequence of nonempty strings")
    try:
        batch = tuple(texts)
    except TypeError as exc:
        raise ValueError("texts must be a sequence of nonempty strings") from exc
    if any(not isinstance(text, str) or not text.strip() for text in batch):
        raise ValueError("texts must be a sequence of nonempty strings")
    if not batch:
        return ()
    # Asymmetric encoders may prepend different query/document prompts. Their
    # declared space must include both preprocessing rules. Legacy providers
    # retain their existing embed method for both roles.
    encode = getattr(provider, "embed_query", provider.embed) if query else provider.embed
    result = await encode(batch)
    if isinstance(result, (str, bytes)):
        raise ValueError("provider must return one vector per text")
    try:
        vectors = tuple(result)
    except TypeError as exc:
        raise ValueError("provider must return one vector per text") from exc
    if len(vectors) != len(batch):
        raise ValueError("provider must return one vector per text")
    return tuple(provider.space.validate(values) for values in vectors)
