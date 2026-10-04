"""Lightweight CPU encoder contracts; CI never downloads weights or imports torch."""

import asyncio
from dataclasses import replace

import pytest

from backend.memory.embedding import EmbeddingSpace, embed_texts
from backend.memory.local_embedding import (
    LocalE5Embeddings,
    LocalEmbeddingContract,
    LocalEmbeddingError,
)
from backend.memory.retrieval import RetrievalResult
from backend.memory.semantic import SemanticMemorySearcher


def test_contract_changes_select_another_embedding_space():
    contract = LocalEmbeddingContract()
    for changed in (
        replace(contract, revision="a" * 40),
        replace(contract, query_prefix="question: "),
        replace(contract, document_prefix="text: "),
        replace(contract, max_tokens=128),
    ):
        assert changed.space.identifier != contract.space.identifier
    for fields in (
        {"revision": "main"},
        {"dimension": 8},
        {"model": "unreviewed/code"},
        {"max_tokens": True},
        {"pooling": "cls"},
        {"normalization": "none"},
    ):
        with pytest.raises(ValueError):
            LocalEmbeddingContract(**fields)


def test_roles_lazy_load_batches_and_close(monkeypatch, tmp_path):
    calls = []
    loads = []
    provider = LocalE5Embeddings(tmp_path)

    def load():
        loads.append(True)

        def encode(texts):
            calls.append(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

        return encode

    monkeypatch.setattr(provider, "_load_encoder", load)

    async def run():
        assert await embed_texts(provider, []) == () and not loads
        await embed_texts(provider, ["人工の記憶"] * 10)
        await embed_texts(provider, ["言い換え質問"], query=True)
        assert loads == [True]
        assert [len(c) for c in calls] == [8, 2, 1]
        assert calls[0][0] == "passage: 人工の記憶"
        assert calls[-1] == ["query: 言い換え質問"]
        await provider.aclose()
        await provider.aclose()
        with pytest.raises(LocalEmbeddingError, match="closed"):
            await provider.embed(["another"])

    asyncio.run(run())


@pytest.mark.parametrize("output", [[], [[1.0]], [[float("nan")] * 384], [[0.0] * 384]])
def test_invalid_output_is_safe_and_retryable(monkeypatch, tmp_path, output):
    provider = LocalE5Embeddings(tmp_path)
    monkeypatch.setattr(provider, "_load_encoder", lambda: lambda _: output)

    async def run():
        with pytest.raises(LocalEmbeddingError, match="could not complete"):
            await provider.embed(["synthetic input"])
        provider._encoder = lambda _: [[1.0] + [0.0] * 383]
        assert len((await provider.embed(["synthetic input"]))[0]) == 384
        await provider.aclose()

    asyncio.run(run())


def test_model_load_failure_hides_paths_and_text_and_can_retry(monkeypatch, tmp_path):
    provider = LocalE5Embeddings(tmp_path)

    def fail():
        raise RuntimeError("synthetic credential/path sentinel")

    monkeypatch.setattr(provider, "_load_encoder", fail)

    async def run():
        with pytest.raises(LocalEmbeddingError) as e:
            await provider.embed(["synthetic input"])
        assert "sentinel" not in str(e.value)
        monkeypatch.setattr(provider, "_load_encoder", lambda: lambda _: [[1.0] + [0.0] * 383])
        assert await provider.embed(["synthetic input"])
        await provider.aclose()

    asyncio.run(run())


def test_semantic_search_uses_query_role_and_preserves_legacy_fallback():
    calls = []

    class Provider:
        space = EmbeddingSpace("fake/role", "v1", 2)

        async def embed(self, texts):
            calls.append("document")
            return [(0.0, 1.0) for _ in texts]

        async def embed_query(self, texts):
            calls.append("query")
            return [(1.0, 0.0) for _ in texts]

    class Retriever:
        vector_index = object()

        async def search_vector(self, query):
            assert query.values == (1.0, 0.0)
            return RetrievalResult((), (), ())

        def verify_current_matches(self, result):
            calls.append("canonical")

    async def run():
        provider = Provider()
        await SemanticMemorySearcher(Retriever(), provider).search("人工query")
        assert calls == ["query", "canonical"]

        class Legacy:
            space = provider.space
            embed = provider.embed

        assert await embed_texts(Legacy(), ["人工query"], query=True) == ((0.0, 1.0),)

    asyncio.run(run())


@pytest.mark.parametrize("output", [[], [(1.0,)], [(float("nan"), 0.0)], "bad"])
def test_query_role_cannot_bypass_vector_validation(output):
    class Provider:
        space = EmbeddingSpace("fake/query", "v1", 2)

        async def embed(self, texts):
            raise AssertionError("query must use the query encoder")

        async def embed_query(self, texts):
            return output

    with pytest.raises(ValueError):
        asyncio.run(embed_texts(Provider(), ["synthetic query"], query=True))
