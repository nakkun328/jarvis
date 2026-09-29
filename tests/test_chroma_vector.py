"""Real local Chroma persistence and vector contract behavior."""

import asyncio
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from backend.memory.chroma import ChromaIndexError, ChromaVectorIndex
from backend.memory.vector import VectorQuery, VectorRecord


def test_spaces_persist_without_storing_memory_content(tmp_path: Path) -> None:
    root = tmp_path / "vectors"
    index = ChromaVectorIndex(root)
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    async def run() -> None:
        await index.upsert(
            [
                VectorRecord("alpha", "model-a-v1", (1.0, 0.0)),
                VectorRecord("beta", "model-a-v1", (0.0, 1.0)),
                VectorRecord("alpha", "model-b-v1", (1.0, 0.0, 0.0)),
            ]
        )
        first = await index.search(VectorQuery("model-a-v1", (1.0, 0.0), limit=2))
        assert [match.memory_id for match in first] == ["alpha", "beta"]
        assert first[0].score > first[1].score
        assert await index.search(VectorQuery("unknown-v1", (1.0, 0.0))) == ()

        reopened = ChromaVectorIndex(root)
        other_space = await reopened.search(VectorQuery("model-b-v1", (1.0, 0.0, 0.0)))
        assert [match.memory_id for match in other_space] == ["alpha"]
        assert reopened.client.count_collections() == 2

        for collection in reopened.client.list_collections():
            stored = collection.get(include=["documents", "metadatas"])
            assert stored["documents"] == [None] * len(stored["ids"])
            assert stored["metadatas"] == [None] * len(stored["ids"])

        await reopened.delete(["alpha"])
        assert [
            match.memory_id
            for match in await reopened.search(VectorQuery("model-a-v1", (1.0, 0.0)))
        ] == ["beta"]
        assert await reopened.search(VectorQuery("model-b-v1", (1.0, 0.0, 0.0))) == ()

    asyncio.run(run())


def test_upsert_replaces_embedding_and_rejects_dimension_mismatch(tmp_path: Path) -> None:
    index = ChromaVectorIndex(tmp_path / "vectors")

    async def run() -> None:
        await index.upsert([VectorRecord("alpha", "model-a-v1", (1.0, 0.0))])
        await index.upsert([VectorRecord("alpha", "model-a-v1", (0.0, 1.0))])
        found = await index.search(VectorQuery("model-a-v1", (0.0, 1.0)))
        assert found[0].memory_id == "alpha"
        assert found[0].score == pytest.approx(0.0)
        with pytest.raises(ValueError, match="dimension"):
            await index.upsert([VectorRecord("beta", "model-a-v1", (0.0, 1.0, 0.0))])
        with pytest.raises(ValueError, match="dimension"):
            await index.search(VectorQuery("model-a-v1", (0.0, 1.0, 0.0)))
        still_indexed = await index.search(VectorQuery("model-a-v1", (0.0, 1.0)))
        assert [match.memory_id for match in still_indexed] == ["alpha"]

    asyncio.run(run())


def test_rejects_symlinked_storage_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")
    with pytest.raises(ChromaIndexError, match="symlink"):
        ChromaVectorIndex(linked)


def test_rejects_permissive_storage_root(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX directory permissions are unavailable")
    root = tmp_path / "public-vectors"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(ChromaIndexError, match="private"):
        ChromaVectorIndex(root)
