import asyncio

import pytest

from backend.memory.embedding import EmbeddingSpace, embed_texts


def test_space_requires_version_and_positive_dimension() -> None:
    for name, version, dimension in (
        ("", "v1", 2),
        ("model@other", "v1", 2),
        ("model", "", 2),
        ("model", "v 1", 2),
        ("model", "v1", 0),
        ("model", "v1", True),
    ):
        with pytest.raises(ValueError):
            EmbeddingSpace(name, version, dimension)


def test_space_validates_record_and_query_dimensions() -> None:
    space = EmbeddingSpace("local/model", "2026-09-29", 2)
    record = space.record("memory-1", [1, 0.5])
    query = space.query([0.5, 1], limit=3)
    assert record.space == query.space == "local/model@2026-09-29:d2"
    assert record.values == (1.0, 0.5)
    assert query.values == (0.5, 1.0)
    for values in ((), (1,), (1, 2, 3), (1, float("nan")), (True, 1)):
        with pytest.raises(ValueError):
            space.record("memory-1", values)
        with pytest.raises(ValueError):
            space.query(values)
    assert EmbeddingSpace("local/model", "v2", 2).identifier != space.identifier
    assert EmbeddingSpace("local/model", "2026-09-29", 3).identifier != space.identifier


def test_embed_texts_checks_provider_cardinality_and_dimension() -> None:
    class FakeProvider:
        space = EmbeddingSpace("fake", "v1", 2)

        def __init__(self, output: object) -> None:
            self.output = output
            self.calls: list[tuple[str, ...]] = []

        async def embed(self, texts: tuple[str, ...]) -> object:
            self.calls.append(texts)
            return self.output

    async def exercise() -> None:
        provider = FakeProvider([(1, 0), (0, 1)])
        assert await embed_texts(provider, [" first ", "second"]) == ((1.0, 0.0), (0.0, 1.0))
        assert provider.calls == [(" first ", "second")]
        assert await embed_texts(provider, []) == ()
        assert len(provider.calls) == 1
        for output in ([], [(1, 0)], [(1,)], [(1, float("inf")), (0, 1)], "bad"):
            with pytest.raises(ValueError):
                await embed_texts(FakeProvider(output), ["first", "second"])
        for texts in ("first", [""], ["  "], [None]):
            with pytest.raises(ValueError):
                await embed_texts(provider, texts)

    asyncio.run(exercise())
