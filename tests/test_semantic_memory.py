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


def test_semantic_query_filters_unreviewed_ids_and_reads_current_note(tmp_path: Path) -> None:
    repository, vault, writer, provider, index, searcher, builder = _system(tmp_path)
    approved = _record("observatory opens Friday")
    unrelated = _record("coffee beans are stored in the kitchen")
    pending = _record("unreviewed observatory rumor")
    conflicted = _record("disputed observatory rumor")
    for record in (approved, unrelated, pending, conflicted):
        writer.submit(record)
    writer.approve(approved.id)
    writer.approve(unrelated.id)
    repository.transition(
        conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
    )

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
        result = await searcher.search("orbit question", limit=1)
        assert [item.record.id for item in result.matches] == [approved.id]
        assert result.matches[0].record.content == "observatory opens Sunday"
        assert result.matches[0].edited_since_approval
        assert [item.record.id for item in result.conflicts] == [conflicted.id]
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
