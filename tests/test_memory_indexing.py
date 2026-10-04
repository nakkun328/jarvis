"""Rebuild the derived Chroma cache from reviewed, current vault notes."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.embedding import EmbeddingSpace
from backend.memory.indexing import IndexBuildError, MemoryIndexBuilder
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.vector import VectorRecord
from backend.memory.writer import MemoryWriter


class FakeProvider:
    space = EmbeddingSpace("fake/local", "v1", 2)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return [(1.0, 0.0) if "Edited" in text else (0.0, 1.0) for text in texts]


def _setup(tmp_path: Path):
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    provider = FakeProvider()
    index = ChromaVectorIndex(tmp_path / "fresh-index")
    builder = MemoryIndexBuilder(repository, MemoryRetriever(repository, vault), provider, index)
    return repository, vault, writer, provider, index, builder


def _record(content: str) -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source="conversation:test",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.7,
        confidence=0.9,
    )


def test_build_uses_current_approved_note_and_verifies_exact_ids(tmp_path: Path) -> None:
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Original approved content")
    pending = _record("Pending content")
    conflicted = _record("Conflicted content")
    for record in (approved, pending, conflicted):
        writer.submit(record)
    writer.approve(approved.id)
    repository.transition(conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)
    note = vault.read(approved.id)
    assert note is not None
    note.path.write_text(
        note.path.read_text(encoding="utf-8").replace("Original", "Edited"),
        encoding="utf-8",
    )

    async def run() -> None:
        report = await builder.populate_empty(batch_size=1)
        assert report.space == provider.space.identifier
        assert report.memory_ids == (str(approved.id),)
        assert report.count == 1
        assert provider.calls == [("Edited approved content",)]
        assert await index.list_ids(provider.space.identifier) == report.memory_ids
        matches = await index.search(provider.space.query((1.0, 0.0)))
        assert [match.memory_id for match in matches] == [str(approved.id)]
        with pytest.raises(IndexBuildError, match="not empty"):
            await builder.populate_empty()
        assert provider.calls == [("Edited approved content",)]

    asyncio.run(run())


def test_missing_approved_note_fails_before_embedding_or_indexing(tmp_path: Path) -> None:
    _, vault, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    note = vault.read(approved.id)
    assert note is not None
    note.path.unlink()

    async def run() -> None:
        with pytest.raises(IndexBuildError, match="needs repair"):
            await builder.populate_empty()
        with pytest.raises(IndexBuildError, match="needs repair"):
            await builder.audit_ids()
        assert provider.calls == []
        assert await index.list_ids(provider.space.identifier) == ()

    asyncio.run(run())


def test_id_audit_reports_missing_and_extra_without_changing_index(tmp_path: Path) -> None:
    _, _, writer, provider, index, builder = _setup(tmp_path)
    first = _record("First approved note")
    second = _record("Second approved note")
    stray = _record("Unreviewed stray note")
    for record in (first, second, stray):
        writer.submit(record)
    writer.approve(first.id)

    async def run() -> None:
        initial = await builder.populate_empty()
        assert initial.memory_ids == (str(first.id),)
        assert (await builder.audit_ids()).healthy
        writer.approve(second.id)
        await index.upsert(
            (VectorRecord(str(stray.id), provider.space.identifier, (1.0, 0.0)),)
        )
        calls_before = list(provider.calls)
        ids_before = await index.list_ids(provider.space.identifier)
        report = await builder.audit_ids()
        assert report.space == provider.space.identifier
        assert report.approved_ids == tuple(sorted((str(first.id), str(second.id))))
        assert report.indexed_ids == ids_before
        assert report.missing_ids == (str(second.id),)
        assert report.extra_ids == (str(stray.id),)
        assert not report.healthy
        assert provider.calls == calls_before
        assert await index.list_ids(provider.space.identifier) == ids_before

    asyncio.run(run())


def test_note_edit_during_embedding_fails_before_upsert(tmp_path: Path) -> None:
    _, vault, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    note = vault.read(approved.id)
    assert note is not None

    async def edit_during_embedding(texts):
        note.path.write_text(
            note.path.read_text(encoding="utf-8").replace("Approved", "Edited"),
            encoding="utf-8",
        )
        return [(0.0, 1.0)]

    provider.embed = edit_during_embedding

    async def run() -> None:
        with pytest.raises(IndexBuildError, match="changed during"):
            await builder.populate_empty()
        assert await index.list_ids(provider.space.identifier) == ()

    asyncio.run(run())


def test_new_approval_during_embedding_fails_before_upsert(tmp_path: Path) -> None:
    _, _, writer, provider, index, builder = _setup(tmp_path)
    original = _record("Original approved content")
    newly_approved = _record("New approved content")
    writer.submit(original)
    writer.submit(newly_approved)
    writer.approve(original.id)

    async def approve_during_embedding(texts):
        writer.approve(newly_approved.id)
        return [(0.0, 1.0) for _ in texts]

    provider.embed = approve_during_embedding

    async def run() -> None:
        with pytest.raises(IndexBuildError, match="changed during"):
            await builder.populate_empty()
        assert await index.list_ids(provider.space.identifier) == ()

    asyncio.run(run())


def test_invalid_provider_dimension_leaves_fresh_index_empty(tmp_path: Path) -> None:
    _, _, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)

    async def wrong_dimension(texts):
        return [(1.0,) for _ in texts]

    provider.embed = wrong_dimension

    async def run() -> None:
        with pytest.raises(ValueError, match="dimension"):
            await builder.populate_empty()
        assert await index.list_ids(provider.space.identifier) == ()

    asyncio.run(run())


def test_manual_refresh_replaces_vector_from_current_approved_note(tmp_path: Path) -> None:
    _, vault, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Original content")
    pending = _record("Pending content")
    writer.submit(approved)
    writer.submit(pending)
    writer.approve(approved.id)

    async def run() -> None:
        await builder.populate_empty()
        with pytest.raises(IndexBuildError, match="not a valid approved"):
            await builder.refresh_approved(pending.id)
        note = vault.read(approved.id)
        assert note is not None
        note.path.write_text(
            note.path.read_text(encoding="utf-8").replace("Original", "Edited"),
            encoding="utf-8",
        )
        await builder.refresh_approved(approved.id)
        assert provider.calls == [("Original content",), ("Edited content",)]
        assert await index.list_ids(provider.space.identifier) == (str(approved.id),)
        matches = await index.search(provider.space.query((1.0, 0.0)))
        assert matches[0].memory_id == str(approved.id)
        assert matches[0].score == pytest.approx(0.0)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["populate", "refresh", "audit"])
@pytest.mark.parametrize("change", ["name", "version"])
def test_contract_change_during_operation_preserves_active_index_and_allows_retry(
    tmp_path: Path, operation: str, change: str
) -> None:
    repository, vault, writer, provider, active, builder = _setup(tmp_path)
    approved = _record("Original approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    original_space = provider.space
    changed_space = EmbeddingSpace(
        "fake/other" if change == "name" else original_space.name,
        "v2" if change == "version" else original_space.version,
        original_space.dimension,
    )
    original_embed = provider.embed

    async def run() -> None:
        await builder.populate_empty()
        before = await active.search(original_space.query((0.0, 1.0)))
        original_note = vault.read(approved.id)
        assert original_note is not None
        candidate = ChromaVectorIndex(tmp_path / "candidate-index")
        target = (
            MemoryIndexBuilder(repository, MemoryRetriever(repository, vault), provider, candidate)
            if operation == "populate"
            else builder
        )
        original_ids = active.list_ids

        async def drift_embed(texts):
            values = await original_embed(texts)
            provider.space = changed_space
            return values

        async def drift_ids(space):
            ids = await original_ids(space)
            provider.space = changed_space
            return ids

        if operation == "audit":
            active.list_ids = drift_ids
        else:
            provider.embed = drift_embed
        with pytest.raises(IndexBuildError, match="contract"):
            if operation == "populate":
                await target.populate_empty()
            elif operation == "refresh":
                await target.refresh_approved(approved.id)
            else:
                await target.audit_ids()
        # No active reader switch or canonical mutation on a rejected contract.
        assert await active.search(original_space.query((0.0, 1.0))) == before
        assert vault.read(approved.id) == original_note
        assert repository.get(approved.id).status is MemoryStatus.APPROVED
        assert await candidate.list_ids(original_space.identifier) == ()
        assert await candidate.list_ids(changed_space.identifier) == ()
        provider.space = original_space
        provider.embed = original_embed
        active.list_ids = original_ids
        if operation == "populate":
            report = await target.populate_empty()
            assert report.memory_ids == (str(approved.id),)
        elif operation == "refresh":
            await target.refresh_approved(approved.id)
        else:
            assert (await target.audit_ids()).healthy
        assert vault.read(approved.id) == original_note

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["second-batch", "partial-upsert"])
def test_failed_private_rebuild_preserves_active_index_and_retries_fresh(
    tmp_path: Path, failure: str
) -> None:
    repository, vault, writer, provider, active, builder = _setup(tmp_path)
    records = [_record("First approved content"), _record("Edited approved content")]
    for record in records:
        writer.submit(record)
        writer.approve(record.id)
    space = provider.space
    candidate = ChromaVectorIndex(tmp_path / "failed-candidate")
    target = MemoryIndexBuilder(
        repository, MemoryRetriever(repository, vault), provider, candidate
    )
    original_embed = provider.embed
    original_upsert = candidate.upsert
    batches = 0

    async def fail_second_batch(texts):
        nonlocal batches
        batches += 1
        if batches == 2:
            raise RuntimeError("synthetic encoder failure")
        return await original_embed(texts)

    async def partial_upsert(values):
        await original_upsert(values[:1])
        raise RuntimeError("synthetic cache failure")

    async def run() -> None:
        await builder.populate_empty()
        before_ids = await active.list_ids(space.identifier)
        before_matches = {
            m.memory_id: m for m in await active.search(space.query((0.0, 1.0)))
        }
        before_notes = {r.id: vault.read(r.id) for r in records}
        if failure == "second-batch":
            provider.embed = fail_second_batch
        else:
            candidate.upsert = partial_upsert
        with pytest.raises(RuntimeError, match="synthetic"):
            await target.populate_empty(batch_size=1)
        assert await active.list_ids(space.identifier) == before_ids
        assert {
            m.memory_id: m for m in await active.search(space.query((0.0, 1.0)))
        } == before_matches
        for record in records:
            assert vault.read(record.id) == before_notes[record.id]
            assert repository.get(record.id).status is MemoryStatus.APPROVED
        failed_ids = await candidate.list_ids(space.identifier)
        assert len(failed_ids) == (0 if failure == "second-batch" else 1)
        provider.embed = original_embed
        if failed_ids:
            with pytest.raises(IndexBuildError, match="not empty"):
                await target.populate_empty()
        retry = ChromaVectorIndex(tmp_path / "retry-candidate")
        report = await MemoryIndexBuilder(
            repository, MemoryRetriever(repository, vault), provider, retry
        ).populate_empty(batch_size=1)
        assert report.memory_ids == before_ids
        assert await active.list_ids(space.identifier) == before_ids
        for record in records:
            assert vault.read(record.id) == before_notes[record.id]

    asyncio.run(run())
