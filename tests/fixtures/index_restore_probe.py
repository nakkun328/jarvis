"""Build/reopen a disposable restored Chroma index in a fresh interpreter."""

import asyncio
import json
import sys
from pathlib import Path

from backend.core.database import Database
from backend.memory.chroma import ChromaVectorIndex
from backend.memory.embedding import EmbeddingSpace
from backend.memory.indexing import MemoryIndexBuilder
from backend.memory.obsidian import ObsidianVault
from backend.memory.repository import MemoryRepository
from backend.memory.retrieval import MemoryRetriever


class FakeProvider:
    space = EmbeddingSpace("restore/fake", "v1", 2)

    async def embed(self, texts):
        return [(1.0, 0.0) for _ in texts]


async def probe(root: Path, mode: str) -> dict:
    database = Database(root / "memory.sqlite3")
    database.initialize()
    repository = MemoryRepository(database)
    vault = ObsidianVault(root / "vault")
    index = ChromaVectorIndex(root / "rebuilt-index")
    retriever = MemoryRetriever(repository, vault, vector_index=index)
    provider = FakeProvider()
    builder = MemoryIndexBuilder(repository, retriever, provider, index)
    if mode == "build":
        assert await index.list_spaces() == ()
        await builder.populate_empty()
    else:
        assert mode == "reopen"
    assert (await builder.audit_ids()).healthy
    matches = await retriever.search_vector(provider.space.query((1.0, 0.0)))
    return {
        "ids": await index.list_ids(provider.space.identifier),
        "entries": await index.list_entries(provider.space.identifier),
        "content": [item.record.content for item in matches.matches],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe(Path(sys.argv[1]), sys.argv[2]))))
