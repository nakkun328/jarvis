"""Build a fresh derived vector space from current approved memory notes."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.memory.embedding import EmbeddingProvider, EmbeddingSpace, embed_texts
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever, RetrievedMemory
from backend.memory.vector import VectorIndex


class IndexBuildError(RuntimeError):
    """A proposed index is incomplete or its canonical source needs repair."""


class InspectableVectorIndex(VectorIndex, Protocol):
    """A vector index that can verify all IDs in one embedding space."""

    async def list_ids(self, space: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class IndexBuildReport:
    space: str
    memory_ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.memory_ids)


class MemoryIndexBuilder:
    """Populate an empty index space and verify it before callers switch to it.

    A new index path prevents stale IDs from an earlier build from surviving.
    SQLite and the current Obsidian notes remain authoritative if this build
    fails or a human edits a note after it completes.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        retriever: MemoryRetriever,
        provider: EmbeddingProvider,
        index: InspectableVectorIndex,
    ) -> None:
        if repository.database.path != retriever.repository.database.path:
            raise ValueError("Repository and retriever must use the same database")
        if not isinstance(provider.space, EmbeddingSpace):
            raise ValueError("provider must declare an embedding space")
        self.repository = repository
        self.retriever = retriever
        self.provider = provider
        self.index = index

    async def populate_empty(self, *, batch_size: int = 64) -> IndexBuildReport:
        """Read every approved note, encode it, then inspect indexed IDs."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        space = self.provider.space
        if await self.index.list_ids(space.identifier):
            raise IndexBuildError("Vector space is not empty; use a new index path")

        approved = self._approved_snapshot()
        records = []
        for start in range(0, len(approved), batch_size):
            batch = approved[start : start + batch_size]
            vectors = await embed_texts(self.provider, [item.record.content for item in batch])
            records.extend(
                space.record(str(item.record.id), values)
                for item, values in zip(batch, vectors, strict=True)
            )

        # An edit made while embedding would make this candidate index stale.
        for item in approved:
            current = self.retriever.get_approved(item.record.id)
            if (
                not isinstance(current, RetrievedMemory)
                or current.note_revision != item.note_revision
            ):
                raise IndexBuildError("Approved note changed during index build")

        await self.index.upsert(records)
        expected = tuple(sorted(record.memory_id for record in records))
        actual = await self.index.list_ids(space.identifier)
        if actual != expected:
            raise IndexBuildError("Vector IDs differ from approved canonical memories")
        return IndexBuildReport(space.identifier, actual)

    async def refresh_approved(self, memory_id: UUID) -> None:
        """Upsert one explicitly approved, current note after a reviewed change."""
        if not isinstance(memory_id, UUID):
            raise ValueError("memory_id must be a UUID")
        current = self.retriever.get_approved(memory_id)
        if not isinstance(current, RetrievedMemory):
            raise IndexBuildError("Memory is not a valid approved note")
        values = (await embed_texts(self.provider, [current.record.content]))[0]
        latest = self.retriever.get_approved(memory_id)
        if not isinstance(latest, RetrievedMemory) or latest.note_revision != current.note_revision:
            raise IndexBuildError("Approved note changed during index refresh")
        await self.index.upsert((self.provider.space.record(str(memory_id), values),))
        if str(memory_id) not in await self.index.list_ids(self.provider.space.identifier):
            raise IndexBuildError("Refreshed memory ID is missing from vector index")

    def _approved_snapshot(self) -> tuple[RetrievedMemory, ...]:
        result: list[RetrievedMemory] = []
        after_id: UUID | None = None
        while True:
            page = self.repository.page_by_status(MemoryStatus.APPROVED, after_id=after_id)
            if not page:
                break
            for stored in page:
                current = self.retriever.get_approved(stored.record.id)
                if not isinstance(current, RetrievedMemory):
                    raise IndexBuildError(
                        f"Approved note {stored.record.id} needs repair before indexing"
                    )
                result.append(current)
            after_id = page[-1].record.id
        return tuple(result)
