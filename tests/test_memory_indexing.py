"""Rebuild the derived Chroma cache from reviewed, current vault notes."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("chromadb")

from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.consolidation import (
    ConversationEvidence,
    IndexRefreshError,
    MemoryConsolidator,
)
from backend.memory.embedding import EmbeddingSpace
from backend.memory.indexing import (
    IndexBuildError,
    MemoryIndexBuilder,
    SynchronousIndexRefresher,
)
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
        assert await index.list_entries(provider.space.identifier) == (
            (str(approved.id), vault.read(approved.id).revision),
        )
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
        await index.upsert((VectorRecord(str(stray.id), provider.space.identifier, (1.0, 0.0)),))
        calls_before = list(provider.calls)
        ids_before = await index.list_ids(provider.space.identifier)
        report = await builder.audit_ids()
        assert report.space == provider.space.identifier
        assert report.approved_ids == tuple(sorted((str(first.id), str(second.id))))
        assert report.indexed_ids == ids_before
        assert report.missing_ids == (str(second.id),)
        assert report.extra_ids == (str(stray.id),)
        assert report.stale_ids == ()
        assert report.untracked_ids == ()
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
        stale = await builder.audit_ids()
        assert stale.stale_ids == (str(approved.id),)
        assert not stale.healthy
        await builder.refresh_approved(approved.id)
        assert (await builder.audit_ids()).healthy
        assert provider.calls == [("Original content",), ("Edited content",)]
        assert await index.list_ids(provider.space.identifier) == (str(approved.id),)
        matches = await index.search(provider.space.query((1.0, 0.0)))
        assert matches[0].memory_id == str(approved.id)
        assert matches[0].score == pytest.approx(0.0)

    asyncio.run(run())


def test_audit_marks_legacy_vector_without_revision_untracked(tmp_path: Path) -> None:
    _, _, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)

    async def run() -> None:
        await index.upsert((VectorRecord(str(approved.id), provider.space.identifier, (0.0, 1.0)),))
        report = await builder.audit_ids()
        assert report.missing_ids == report.extra_ids == report.stale_ids == ()
        assert report.untracked_ids == (str(approved.id),)
        assert not report.healthy
        await builder.refresh_approved(approved.id)
        assert (await builder.audit_ids()).healthy

    asyncio.run(run())


def test_note_edit_during_index_write_is_detected(tmp_path: Path) -> None:
    _, vault, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    note = vault.read(approved.id)
    assert note is not None
    original_upsert = index.upsert

    async def edit_after_upsert(records):
        await original_upsert(records)
        note.path.write_text(
            note.path.read_text(encoding="utf-8").replace("Approved", "Edited"),
            encoding="utf-8",
        )

    index.upsert = edit_after_upsert

    async def run() -> None:
        with pytest.raises(IndexBuildError, match="changed during"):
            await builder.populate_empty()
        assert (await builder.audit_ids()).stale_ids == (str(approved.id),)

    asyncio.run(run())


def test_note_edit_during_audit_cannot_report_healthy(tmp_path: Path) -> None:
    _, vault, writer, _, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    asyncio.run(builder.populate_empty())
    note = vault.read(approved.id)
    assert note is not None
    original_list_entries = index.list_entries

    async def edit_during_list(space):
        entries = await original_list_entries(space)
        note.path.write_text(
            note.path.read_text(encoding="utf-8").replace("Approved", "Edited"),
            encoding="utf-8",
        )
        return entries

    index.list_entries = edit_during_list

    async def run() -> None:
        with pytest.raises(IndexBuildError, match="changed during index audit"):
            await builder.audit_ids()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["refresh", "audit"])
@pytest.mark.parametrize("change", ["retirement", "revision"])
def test_change_inside_final_index_resolution_fails_and_can_retry(tmp_path, operation, change):
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    asyncio.run(builder.populate_empty())
    original_resolve = builder.retriever._resolve
    calls = 0
    changed = False

    def interleave(stored, now):
        nonlocal calls, changed
        resolved = original_resolve(stored, now)
        calls += 1
        # The final refresh/audit resolve has read SQLite and the old note,
        # but has not returned that earlier snapshot to the builder yet.
        if calls == (3 if operation == "refresh" else 2):
            note = vault.read(approved.id)
            if change == "retirement":
                writer.retire(approved.id, actor="synthetic-test", reason="Fixture review")
            else:
                vault.update(
                    approved.id, "Edited content", note.metadata, expected_revision=note.revision
                )
            changed = True
        return resolved

    builder.retriever._resolve = interleave

    async def run():
        with pytest.raises(IndexBuildError, match="changed|repair"):
            if operation == "refresh":
                await builder.refresh_approved(approved.id)
            else:
                await builder.audit_ids()
        assert changed
        # Never undo the canonical review/edit just to keep a derived cache valid.
        note = vault.read(approved.id)
        assert note is not None
        if change == "retirement":
            assert repository.get(approved.id).status is MemoryStatus.RETIRED
            assert (await builder.audit_ids()).extra_ids == (str(approved.id),)
            await builder.remove_inactive(approved.id)
        else:
            assert note.body == "Edited content"
            assert (await builder.audit_ids()).stale_ids == (str(approved.id),)
            await builder.refresh_approved(approved.id)
        assert (await builder.audit_ids()).healthy
        assert vault.read(approved.id) is not None

    asyncio.run(run())


def test_inactive_cleanup_requires_terminal_review_state(tmp_path: Path) -> None:
    repository, _, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)
    asyncio.run(builder.populate_empty())
    other_space = "fake/local@v2:d2"
    asyncio.run(index.upsert((VectorRecord(str(approved.id), other_space, (0.0, 1.0)),)))

    async def rejected_cleanup() -> None:
        with pytest.raises(IndexBuildError, match="not superseded or retired"):
            await builder.remove_inactive(approved.id)
        assert await index.list_ids(provider.space.identifier) == (str(approved.id),)

    asyncio.run(rejected_cleanup())

    # The lifecycle PR supplies these terminal states. Exercise the index
    # cleanup here without duplicating its schema or transition implementation.
    repository.get = lambda _id: SimpleNamespace(status=SimpleNamespace(value="superseded"))
    asyncio.run(builder.remove_inactive(approved.id))
    assert asyncio.run(index.list_ids(provider.space.identifier)) == ()
    assert asyncio.run(index.list_ids(other_space)) == ()


def test_inactive_cleanup_detects_partial_delete_across_spaces(tmp_path: Path) -> None:
    repository, _, writer, provider, index, builder = _setup(tmp_path)
    approved = _record("Approved content")
    writer.submit(approved)
    writer.approve(approved.id)

    async def run() -> None:
        await builder.populate_empty()
        other_space = "fake/local@v2:d2"
        await index.upsert((VectorRecord(str(approved.id), other_space, (0.0, 1.0)),))
        assert await index.list_spaces() == tuple(sorted((provider.space.identifier, other_space)))
        repository.get = lambda _id: SimpleNamespace(status=SimpleNamespace(value="retired"))

        async def partial_delete(memory_ids):
            for collection in index.client.list_collections():
                if (collection.metadata or {}).get("space") == provider.space.identifier:
                    collection.delete(ids=list(memory_ids))

        index.delete = partial_delete
        with pytest.raises(IndexBuildError, match="remains in vector index"):
            await builder.remove_inactive(approved.id)
        assert await index.list_ids(provider.space.identifier) == ()
        assert await index.list_ids(other_space) == (str(approved.id),)

    asyncio.run(run())


def test_reviewed_correction_and_retirement_clean_real_index(tmp_path: Path) -> None:
    if not hasattr(MemoryStatus, "SUPERSEDED"):
        pytest.skip("Requires the reviewed lifecycle PR #30")
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    retriever = MemoryRetriever(repository, vault)
    pipeline = MemoryConsolidator(
        repository.database,
        writer,
        retriever,
        index_refresher=SynchronousIndexRefresher(builder),
    )
    original = _record("Original approved content")
    correction = _record("Corrected approved content")
    writer.submit(original)
    writer.approve(original.id)
    asyncio.run(builder.populate_empty())
    other_space = "fake/local@v2:d2"
    asyncio.run(index.upsert((VectorRecord(str(original.id), other_space, (0.0, 1.0)),)))
    writer.submit_correction(original.id, correction)

    original_delete = index.delete
    failed = False

    async def fail_once(memory_ids):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("private index failure")
        await original_delete(memory_ids)

    index.delete = fail_once
    with pytest.raises(IndexRefreshError, match="retry publish_reviewed") as error:
        pipeline.publish_reviewed(correction.id, actor="reviewer:alice")
    assert "private index failure" not in str(error.value)
    assert repository.get(original.id).status is MemoryStatus.SUPERSEDED
    assert vault.read(original.id).body == original.content
    stale = asyncio.run(builder.audit_ids())
    assert stale.extra_ids == (str(original.id),)
    assert not stale.healthy

    pipeline.publish_reviewed(correction.id, actor="reviewer:alice")
    assert asyncio.run(builder.audit_ids()).healthy
    assert asyncio.run(index.list_ids(provider.space.identifier)) == (str(correction.id),)
    assert asyncio.run(index.list_ids(other_space)) == ()
    assert len(repository.lifecycle_events(original.id)) == 1
    assert len(repository.review_events(correction.id)) == 1
    assert retriever.get_approved(original.id) is None
    asyncio.run(index.upsert((VectorRecord(str(correction.id), other_space, (0.0, 1.0)),)))
    pipeline.retire_reviewed(correction.id, actor="reviewer:alice", reason="Outdated")
    assert asyncio.run(index.list_ids(provider.space.identifier)) == ()
    assert asyncio.run(index.list_ids(other_space)) == ()
    assert len(repository.lifecycle_events(correction.id)) == 1
    assert asyncio.run(builder.audit_ids()).healthy
    assert vault.read(correction.id).body == correction.content


def test_real_multispace_cleanup_failure_preserves_retirement_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    record = _record("Reviewed memory to retire")
    writer.submit(record)
    writer.approve(record.id, actor="reviewer")
    revision = vault.read(record.id).revision
    spaces = (provider.space, EmbeddingSpace("fake/local", "cleanup-v2", 2))
    asyncio.run(
        index.upsert(
            [space.record(str(record.id), (1.0, 0.0), source_revision=revision) for space in spaces]
        )
    )
    retriever = MemoryRetriever(repository, vault, vector_index=index)
    pipeline = MemoryConsolidator(
        repository.database,
        writer,
        retriever,
        index_refresher=SynchronousIndexRefresher(builder),
    )
    deleted = 0
    get_collection = index.client.get_collection

    class FlakyCollection:
        def __init__(self, collection):
            self.collection = collection

        def __getattr__(self, name):
            return getattr(self.collection, name)

        def delete(self, **kwargs):
            nonlocal deleted
            deleted += 1
            if deleted == 2:
                raise OSError("simulated cache failure")
            return self.collection.delete(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            index.client,
            "get_collection",
            lambda **kwargs: FlakyCollection(get_collection(**kwargs)),
        )
        with pytest.raises(IndexRefreshError, match="retry retire_reviewed") as failure:
            pipeline.retire_reviewed(record.id, actor="reviewer", reason="obsolete")
    assert "simulated cache failure" not in str(failure.value)
    assert deleted == 2
    assert sorted([len(asyncio.run(index.list_ids(space.identifier))) for space in spaces]) == [
        0,
        1,
    ]
    assert repository.get(record.id).status is MemoryStatus.RETIRED
    assert vault.read(record.id).revision == revision
    for space in spaces:
        assert asyncio.run(retriever.search_vector(space.query((1.0, 0.0)))).matches == ()
    pipeline.retire_reviewed(record.id, actor="reviewer", reason="obsolete")
    for space in spaces:
        assert asyncio.run(index.list_ids(space.identifier)) == ()
    assert asyncio.run(builder.audit_ids()).healthy
    assert len(repository.lifecycle_events(record.id)) == 1
    assert len(repository.review_events(record.id)) == 1


def test_reviewed_publication_refreshes_real_chroma_index(tmp_path: Path) -> None:
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    retriever = MemoryRetriever(repository, vault)
    pipeline = MemoryConsolidator(
        repository.database,
        writer,
        retriever,
        index_refresher=SynchronousIndexRefresher(builder),
    )
    staged = pipeline.stage(
        [ConversationEvidence(uuid4(), 1, "user", "Remember reply_style: concise replies")]
    )
    assert len(staged.pending) == 1
    memory_id = staged.pending[0].record.id
    assert asyncio.run(index.list_ids(provider.space.identifier)) == ()

    approved = pipeline.publish_reviewed(memory_id)
    assert approved.status is MemoryStatus.APPROVED
    assert provider.calls == [("concise replies",)]
    assert asyncio.run(index.list_ids(provider.space.identifier)) == (str(memory_id),)
    matches = asyncio.run(index.search(provider.space.query((0.0, 1.0))))
    assert [match.memory_id for match in matches] == [str(memory_id)]
    assert retriever.get_approved(memory_id).record.content == "concise replies"


def test_failed_refresh_keeps_approval_and_can_retry(tmp_path: Path) -> None:
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    pipeline = MemoryConsolidator(
        repository.database,
        writer,
        MemoryRetriever(repository, vault),
        index_refresher=SynchronousIndexRefresher(builder),
    )
    staged = pipeline.stage(
        [ConversationEvidence(uuid4(), 1, "user", "Remember reply_style: concise replies")]
    )
    memory_id = staged.pending[0].record.id
    original_embed = provider.embed

    async def wrong_dimension(_texts):
        return [(1.0,)]

    provider.embed = wrong_dimension
    with pytest.raises(IndexRefreshError, match="was approved"):
        pipeline.publish_reviewed(memory_id)
    assert repository.get(memory_id).status is MemoryStatus.APPROVED
    note = vault.read(memory_id)
    assert note is not None
    assert asyncio.run(index.list_ids(provider.space.identifier)) == ()

    provider.embed = original_embed
    pipeline.publish_reviewed(memory_id)
    assert vault.read(memory_id).revision == note.revision
    assert asyncio.run(index.list_ids(provider.space.identifier)) == (str(memory_id),)


def test_restored_vault_rebuilds_fresh_index_and_reopens_in_another_process(tmp_path: Path) -> None:
    repository, vault, writer, provider, index, builder = _setup(tmp_path)
    original, current, retired, pending = [
        _record(text) for text in ("Old observatory", "New observatory", "Retired", "Pending")
    ]
    for record in (original, retired, pending):
        writer.submit(record)
    writer.approve(original.id)
    writer.approve(retired.id)
    asyncio.run(builder.populate_empty())
    writer.submit_correction(original.id, current)
    writer.approve(current.id)
    writer.retire(retired.id, actor="reviewer", reason="obsolete")
    note = vault.read(current.id)
    assert note is not None
    note.path.write_text(note.path.read_text().replace("New observatory", "Edited observatory"))
    revision = vault.read(current.id).revision
    # The old cache is intentionally stale; recovery must not copy it.
    assert asyncio.run(index.list_ids(provider.space.identifier)) == tuple(
        sorted((str(original.id), str(retired.id)))
    )
    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    with repository.database.connect() as source:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        with closing(sqlite3.connect(restored / "memory.sqlite3")) as destination:
            source.backup(destination)
    shutil.copytree(vault.root, restored / "vault")
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root) + os.pathsep + environment.get("PYTHONPATH", "")
    expected = {
        "ids": [str(current.id)],
        "entries": [[str(current.id), revision]],
        "content": ["Edited observatory"],
    }
    for mode in ("build", "reopen"):
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "tests/fixtures/index_restore_probe.py"),
                str(restored),
                mode,
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        assert json.loads(result.stdout) == expected
    assert len(repository.review_events(current.id)) == 1
    assert len(repository.lifecycle_events(original.id)) == 1
    assert len(repository.lifecycle_events(retired.id)) == 1
