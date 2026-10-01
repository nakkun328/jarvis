"""Real Chroma candidates are resolved through canonical memory review state."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository, MemoryStatus
from backend.memory.retrieval import MemoryRetriever
from backend.memory.vector import VectorQuery, VectorRecord
from backend.memory.writer import MemoryWriter


def test_chroma_candidates_do_not_hide_approved_memory(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(tmp_path / "vault")
    writer = MemoryWriter(repository, vault)

    def memory(content: str) -> MemoryRecord:
        return MemoryRecord(
            category=MemoryCategory.USER,
            content=content,
            source="conversation:123",
            origin=MemoryOrigin.USER_EXPLICIT,
            importance=0.6,
            confidence=0.8,
        )

    pending = memory("Pending")
    conflicted = memory("Conflicted")
    approved = memory("Approved")
    for record in (pending, conflicted, approved):
        writer.submit(record)
    repository.transition(
        conflicted.id, expected=MemoryStatus.PENDING, new=MemoryStatus.CONFLICT
    )
    writer.approve(approved.id)

    async def run() -> None:
        index = ChromaVectorIndex(tmp_path / "vectors")
        await index.upsert(
            [
                VectorRecord(str(pending.id), "test-model-v1", (1.0, 0.0)),
                VectorRecord(str(conflicted.id), "test-model-v1", (0.9, 0.1)),
                VectorRecord(str(approved.id), "test-model-v1", (0.0, 1.0)),
            ]
        )
        result = await MemoryRetriever(repository, vault, vector_index=index).search_vector(
            VectorQuery("test-model-v1", (1.0, 0.0), limit=1)
        )
        assert [item.record.id for item in result.matches] == [approved.id]
        assert [item.record.id for item in result.conflicts] == [conflicted.id]
        assert result.issues == ()

    asyncio.run(run())
