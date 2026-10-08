"""Semantic query vectors still resolve to reviewed, current vault content."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.embedding import EmbeddingSpace
from backend.memory.indexing import MemoryIndexBuilder
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.semantic import SemanticMemorySearcher
from backend.memory.vector import VectorMatch
from backend.memory.writer import MemoryWriter


class FakeProvider:
    space = EmbeddingSpace("fake-semantic", "v1", 2)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return [
            (1.0, 0.0)
            if text == "orbit question"
            else (0.8, 0.2)
            if "observatory" in text
            else (0.0, 1.0)
            for text in texts
        ]


def _record(content: str) -> MemoryRecord:
    return MemoryRecord(
        category=MemoryCategory.USER,
        content=content,
        source="conversation:example",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.7,
        confidence=0.9,
    )


def _system(tmp_path: Path):
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)
    provider = FakeProvider()
    index = ChromaVectorIndex(tmp_path / "index")
    retriever = MemoryRetriever(repository, vault, vector_index=index)
    searcher = SemanticMemorySearcher(retriever, provider)
    builder = MemoryIndexBuilder(repository, retriever, provider, index)
    return repository, vault, writer, provider, index, searcher, builder


@pytest.mark.parametrize("candidate_order", ["controlled", "chroma"])
def test_semantic_query_filters_unreviewed_ids_and_reads_current_note(
    tmp_path: Path, candidate_order
) -> None:
    repository, vault, writer, provider, index, searcher, builder = _system(tmp_path)
    approved = _record("observatory opens Friday")
    unrelated = _record("coffee beans are stored in the kitchen")
    pending = _record("unreviewed observatory rumor")
    conflicted = _record("disputed observatory rumor")
    for record in (approved, unrelated, pending, conflicted):
        writer.submit(record)
    writer.approve(approved.id)
    writer.approve(unrelated.id)
    repository.transition(conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT)

    async def run() -> None:
        await builder.populate_empty()
        await index.upsert(
            (
                provider.space.record(str(pending.id), (1.0, 0.0)),
                provider.space.record(str(conflicted.id), (0.9, 0.1)),
            )
        )
        note = vault.read(approved.id)
        assert note is not None
        vault.update(
            approved.id,
            "observatory opens Sunday",
            note.metadata,
            expected_revision=note.revision,
        )
        # Exact expansion/conflict expectations belong to controlled ordering;
        # Chroma ANN can omit or reorder a candidate. Keep real persistence in
        # both cases and separately check actual Chroma canonical safety.
        limits = []
        if candidate_order == "controlled":

            class OrderedCandidates:
                async def search(self, query):
                    assert query.space == provider.space.identifier
                    assert query.values == (1.0, 0.0)
                    limits.append(query.limit)
                    rows = (
                        VectorMatch(str(pending.id), 0.0),
                        VectorMatch(str(conflicted.id), -0.02),
                        VectorMatch(str(approved.id), -0.08),
                        VectorMatch(str(unrelated.id), -2.0),
                    )
                    return rows[: query.limit]

            searcher.retriever.vector_index = OrderedCandidates()
        assert set(await index.list_ids(provider.space.identifier)) == {
            str(r.id) for r in (approved, unrelated, pending, conflicted)
        }
        result = await searcher.search("orbit question", limit=1)
        if candidate_order == "controlled":
            assert limits == [1, 2, 4]
            assert [item.record.id for item in result.matches] == [approved.id]
            assert result.matches[0].record.content == "observatory opens Sunday"
            assert result.matches[0].edited_since_approval
            assert [item.record.id for item in result.conflicts] == [conflicted.id]
        # Real ANN results are not an exhaustive conflict inventory or a
        # quality gold pass. Every returned fact must still be current/approved.
        assert len(result.matches) <= 1
        for match in result.matches:
            assert match.record.id in {approved.id, unrelated.id}
            assert repository.get(match.record.id).status is MemoryStatus.APPROVED
            assert match.record.content == vault.read(match.record.id).body
            assert match.record.source == "conversation:example"
            assert match.record.origin is MemoryOrigin.USER_EXPLICIT
            assert match.record.confidence == 0.9
        assert {c.record.id for c in result.conflicts} <= {conflicted.id}
        assert result.issues == ()
        assert provider.calls[-1] == ("orbit question",)

    asyncio.run(run())


def test_invalid_query_fails_before_remote_embedding(tmp_path: Path) -> None:
    _, _, _, provider, _, searcher, _ = _system(tmp_path)

    async def run() -> None:
        with pytest.raises(ValueError, match="query"):
            await searcher.search(" ")
        with pytest.raises(ValueError, match="limit"):
            await searcher.search("orbit question", limit=0)
        with pytest.raises(ValueError, match="query"):
            await searcher.search("x" * 4001)

    asyncio.run(run())
    assert provider.calls == []


@pytest.mark.parametrize("boundary", ["embedding", "index"])
@pytest.mark.parametrize("change", ["name", "version"])
def test_query_contract_drift_fails_without_returning_context_and_can_retry(
    tmp_path: Path, boundary: str, change: str
) -> None:
    repository, vault, writer, provider, index, searcher, builder = _system(tmp_path)
    approved = _record("observatory opens Friday")
    writer.submit(approved)
    writer.approve(approved.id)
    space = provider.space
    changed = EmbeddingSpace(
        "fake-other" if change == "name" else space.name,
        "v2" if change == "version" else space.version,
        space.dimension,
    )
    original_embed = provider.embed
    original_search = index.search
    index_calls = []

    async def drift_embed(texts):
        values = await original_embed(texts)
        provider.space = changed
        return values

    async def drift_search(query):
        index_calls.append(query)
        result = await original_search(query)
        if boundary == "index":
            provider.space = changed
        return result

    async def run() -> None:
        await builder.populate_empty()
        note = vault.read(approved.id)
        if boundary == "embedding":
            provider.embed = drift_embed
        index.search = drift_search
        with pytest.raises(ValueError, match="contract"):
            await searcher.search("orbit question", limit=1)
        if boundary == "embedding":
            assert index_calls == []
        else:
            assert all(query.space == space.identifier for query in index_calls)
        assert vault.read(approved.id) == note
        assert repository.get(approved.id).status is MemoryStatus.APPROVED
        assert await index.list_ids(space.identifier) == (str(approved.id),)
        assert await index.list_ids(changed.identifier) == ()
        provider.space = space
        provider.embed = original_embed
        index.search = original_search
        result = await searcher.search("orbit question", limit=1)
        assert [m.record.id for m in result.matches] == [approved.id]
        assert result.matches[0].record.content == note.body
        assert result.matches[0].record.confidence == 0.9
        assert result.matches[0].record.origin is MemoryOrigin.USER_EXPLICIT
        assert result.issues == ()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["retirement", "revision"])
def test_change_inside_query_resolution_fails_closed_and_can_retry(tmp_path: Path, change: str):
    repository, vault, writer, provider, index, searcher, builder = _system(tmp_path)
    approved = _record("observatory opens Friday")
    writer.submit(approved)
    writer.approve(approved.id)
    asyncio.run(builder.populate_empty())
    original_resolve = searcher.retriever._resolve
    changed = False

    def interleave(stored, now):
        nonlocal changed
        resolved = original_resolve(stored, now)
        if not changed:
            changed = True
            if change == "retirement":
                writer.retire(approved.id, actor="synthetic-reviewer", reason="Fixture retirement")
            else:
                note = vault.read(approved.id)
                vault.update(
                    approved.id,
                    "observatory opens Sunday",
                    note.metadata,
                    expected_revision=note.revision,
                )
        return resolved

    searcher.retriever._resolve = interleave

    async def run():
        with pytest.raises(ValueError, match="changed"):
            await searcher.search("orbit question", limit=1)
        assert changed
        assert await index.list_ids(provider.space.identifier) == (str(approved.id),)
        result = await searcher.search("orbit question", limit=1)
        if change == "retirement":
            assert repository.get(approved.id).status is MemoryStatus.RETIRED
            assert result.matches == ()
            assert vault.read(approved.id) is not None
        else:
            assert [m.record.id for m in result.matches] == [approved.id]
            assert result.matches[0].record.content == "observatory opens Sunday"
            assert result.matches[0].edited_since_approval
            assert result.matches[0].record.confidence == 0.9
        assert result.issues == ()

    asyncio.run(run())
