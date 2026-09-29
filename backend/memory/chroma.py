"""Local persistent Chroma implementation of the rebuildable vector index."""

import asyncio
import hashlib
import os
import stat
import threading
from collections.abc import Sequence
from pathlib import Path

from backend.memory.vector import VectorMatch, VectorQuery, VectorRecord

_PREFIX = "jarvis-"


class ChromaIndexError(RuntimeError):
    """The local vector index could not be used safely."""


class ChromaVectorIndex:
    """Keep only IDs and embeddings in one Chroma collection per embedding space.

    The supplied vectors must come from an explicitly versioned embedding
    provider. Chroma is a derived cache; SQLite and Obsidian own the content.
    """

    def __init__(self, path: Path) -> None:
        try:
            import chromadb
            from chromadb.config import Settings
            from chromadb.errors import NotFoundError
        except ImportError as exc:
            raise ChromaIndexError("Install the vector extra to use Chroma") from exc
        if path.is_symlink():
            raise ChromaIndexError("Vector index directory must not be a symlink")
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ChromaIndexError("Vector index directory is unavailable") from exc
        if path.is_symlink():
            raise ChromaIndexError("Vector index directory must not be a symlink")
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ChromaIndexError("Vector index directory must be private")
        self.client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self._not_found = NotFoundError
        self._lock = threading.RLock()

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        try:
            await asyncio.to_thread(self._upsert, records)
        except ValueError:
            raise
        except Exception as exc:
            raise ChromaIndexError("Could not update vector index") from exc

    async def delete(self, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return
        if any(not isinstance(item, str) or not item.strip() for item in memory_ids):
            raise ValueError("memory_ids must contain nonempty strings")
        try:
            await asyncio.to_thread(self._delete, tuple(set(memory_ids)))
        except Exception as exc:
            raise ChromaIndexError("Could not delete vector records") from exc

    async def search(self, query: VectorQuery) -> Sequence[VectorMatch]:
        try:
            return await asyncio.to_thread(self._search, query)
        except ValueError:
            raise
        except Exception as exc:
            raise ChromaIndexError("Could not search vector index") from exc

    def _upsert(self, records: Sequence[VectorRecord]) -> None:
        groups: dict[str, dict[str, VectorRecord]] = {}
        for record in records:
            groups.setdefault(record.space, {})[record.memory_id] = record
        with self._lock:
            for space, by_id in groups.items():
                items = list(by_id.values())
                dimension = len(items[0].values)
                if any(len(item.values) != dimension for item in items):
                    raise ValueError("Embedding dimensions differ within a space")
                collection = self.client.get_or_create_collection(
                    name=_name(space),
                    metadata={"space": space, "dimension": dimension},
                    embedding_function=None,
                )
                _check_collection(collection, space, dimension)
                batch_size = self.client.get_max_batch_size()
                for start in range(0, len(items), batch_size):
                    batch = items[start : start + batch_size]
                    collection.upsert(
                        ids=[item.memory_id for item in batch],
                        embeddings=[list(item.values) for item in batch],
                    )

    def _delete(self, memory_ids: tuple[str, ...]) -> None:
        with self._lock:
            for item in self.client.list_collections():
                name = getattr(item, "name", item)
                if not isinstance(name, str) or not name.startswith(_PREFIX):
                    continue
                collection = self.client.get_collection(name=name, embedding_function=None)
                metadata = collection.metadata or {}
                space = metadata.get("space")
                if isinstance(space, str) and _name(space) == name:
                    collection.delete(ids=list(memory_ids))

    def _search(self, query: VectorQuery) -> Sequence[VectorMatch]:
        with self._lock:
            try:
                collection = self.client.get_collection(
                    name=_name(query.space), embedding_function=None
                )
            except self._not_found:
                return ()
            _check_collection(collection, query.space, len(query.values))
            count = collection.count()
            if count == 0:
                return ()
            result = collection.query(
                query_embeddings=[list(query.values)],
                n_results=min(query.limit, count),
                include=["distances"],
            )
            ids = result["ids"][0]
            distances = result["distances"][0]
            if distances is None or len(ids) != len(distances):
                raise ChromaIndexError("Invalid Chroma search result")
            return tuple(
                VectorMatch(memory_id=memory_id, score=-float(distance))
                for memory_id, distance in zip(ids, distances, strict=True)
            )


def _name(space: str) -> str:
    digest = hashlib.sha256(space.encode("utf-8")).hexdigest()[:56]
    return f"{_PREFIX}{digest}"


def _check_collection(collection: object, space: str, dimension: int) -> None:
    metadata = collection.metadata or {}
    if metadata.get("space") != space:
        raise ChromaIndexError("Vector collection space does not match")
    if metadata.get("dimension") != dimension:
        raise ValueError("Embedding dimension does not match this space")
