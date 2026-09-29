"""Build and refresh a derived vector space from approved memory notes."""

import asyncio
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
    """A vector index that can verify IDs and spaces in a derived cache."""

    async def list_spaces(self) -> tuple[str, ...]: ...

    async def list_ids(self, space: str) -> tuple[str, ...]: ...

    async def list_entries(self, space: str) -> tuple[tuple[str, str | None], ...]: ...


@dataclass(frozen=True)
class IndexBuildReport:
    space: str
    memory_ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.memory_ids)


@dataclass(frozen=True)
class IndexIdAudit:
    space: str
    approved_ids: tuple[str, ...]
    indexed_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]
    extra_ids: tuple[str, ...]
    stale_ids: tuple[str, ...]
    untracked_ids: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not (
            self.missing_ids or self.extra_ids or self.stale_ids or self.untracked_ids
        )


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
                space.record(
                    str(item.record.id), values, source_revision=item.note_revision
                )
                for item, values in zip(batch, vectors, strict=True)
            )

        # Recheck the whole set: a new approval during embedding would also
        # make this supposedly complete candidate index stale.
        latest = self._approved_snapshot()
        if tuple((item.record.id, item.note_revision) for item in latest) != tuple(
            (item.record.id, item.note_revision) for item in approved
        ):
            raise IndexBuildError("Approved memories changed during index build")

        await self.index.upsert(records)
        expected = tuple(sorted(record.memory_id for record in records))
        actual = await self.index.list_ids(space.identifier)
        if actual != expected:
            raise IndexBuildError("Vector IDs differ from approved canonical memories")
        self._check_revisions(latest, await self.index.list_entries(space.identifier))
        if self._revision_snapshot() != self._revisions(latest):
            raise IndexBuildError("Approved memories changed during index build")
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
        await self.index.upsert(
            (
                self.provider.space.record(
                    str(memory_id), values, source_revision=current.note_revision
                ),
            )
        )
        if str(memory_id) not in await self.index.list_ids(self.provider.space.identifier):
            raise IndexBuildError("Refreshed memory ID is missing from vector index")
        entries = dict(await self.index.list_entries(self.provider.space.identifier))
        if entries.get(str(memory_id)) != current.note_revision:
            raise IndexBuildError("Refreshed memory revision is missing from vector index")
        final = self.retriever.get_approved(memory_id)
        if not isinstance(final, RetrievedMemory) or final.note_revision != current.note_revision:
            raise IndexBuildError("Approved note changed during index refresh")

    async def remove_inactive(self, memory_id: UUID) -> None:
        """Remove a reviewed superseded or retired ID from every derived space."""
        if not isinstance(memory_id, UUID):
            raise ValueError("memory_id must be a UUID")
        stored = self.repository.get(memory_id)
        if stored is None or stored.status.value not in {"superseded", "retired"}:
            raise IndexBuildError("Memory is not superseded or retired")
        spaces_before = await self.index.list_spaces()
        await self.index.delete((str(memory_id),))
        spaces_after = await self.index.list_spaces()
        for space in sorted(set(spaces_before) | set(spaces_after)):
            if str(memory_id) in await self.index.list_ids(space):
                raise IndexBuildError("Inactive memory ID remains in vector index")

    async def audit_ids(self) -> IndexIdAudit:
        """Compare canonical approved IDs with one derived space without mutation.

        Revision checks detect human edits since indexing. They cannot prove
        that the provider returned useful embeddings.
        """
        approved = self._approved_snapshot()
        canonical = tuple(sorted(str(item.record.id) for item in approved))
        entries = await self.index.list_entries(self.provider.space.identifier)
        if self._revision_snapshot() != self._revisions(approved):
            raise IndexBuildError("Approved memories changed during index audit")
        indexed = tuple(memory_id for memory_id, _ in entries)
        revisions = dict(entries)
        current = {str(item.record.id): item.note_revision for item in approved}
        canonical_set = set(canonical)
        indexed_set = set(indexed)
        return IndexIdAudit(
            space=self.provider.space.identifier,
            approved_ids=canonical,
            indexed_ids=indexed,
            missing_ids=tuple(sorted(canonical_set - indexed_set)),
            extra_ids=tuple(sorted(indexed_set - canonical_set)),
            stale_ids=tuple(
                sorted(
                    memory_id for memory_id in canonical_set & indexed_set
                    if revisions[memory_id] is not None
                    and revisions[memory_id] != current[memory_id]
                )
            ),
            untracked_ids=tuple(
                sorted(
                    memory_id for memory_id in canonical_set & indexed_set
                    if revisions[memory_id] is None
                )
            ),
        )

    def _revision_snapshot(self) -> tuple[tuple[UUID, str], ...]:
        return self._revisions(self._approved_snapshot())

    @staticmethod
    def _revisions(items: tuple[RetrievedMemory, ...]) -> tuple[tuple[UUID, str], ...]:
        return tuple((item.record.id, item.note_revision) for item in items)

    def _check_revisions(
        self,
        approved: tuple[RetrievedMemory, ...],
        entries: tuple[tuple[str, str | None], ...],
    ) -> None:
        expected = {str(item.record.id): item.note_revision for item in approved}
        if dict(entries) != expected:
            raise IndexBuildError("Vector revisions differ from approved canonical memories")

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


class SynchronousIndexRefresher:
    """Bridge offline, explicit publication to the async index builder.

    Run this from a synchronous worker or CLI. An async caller should move the
    publication operation to a worker thread rather than nest an event loop.
    """

    def __init__(self, builder: MemoryIndexBuilder) -> None:
        self.builder = builder

    def refresh(self, memory: RetrievedMemory) -> None:
        if not isinstance(memory, RetrievedMemory):
            raise ValueError("refresh requires a resolved approved memory")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Run synchronous index refresh outside an event loop")
        current = self.builder.retriever.get_approved(memory.record.id)
        if (
            not isinstance(current, RetrievedMemory)
            or current.note_revision != memory.note_revision
        ):
            raise IndexBuildError("Approved note changed before index refresh")
        asyncio.run(self.builder.refresh_approved(memory.record.id))

    def remove_inactive(self, memory_id: UUID) -> None:
        """Retryable cleanup after a reviewed correction or retirement."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Run synchronous index cleanup outside an event loop")
        asyncio.run(self.builder.remove_inactive(memory_id))
