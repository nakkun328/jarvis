import asyncio

import pytest

from backend.memory.vector import VectorMatch, VectorQuery, VectorRecord


def test_vector_contract_rejects_invalid_inputs() -> None:
    for values in ((), (float("nan"),), (float("inf"),), (True,), ("1",), (10**1000,)):
        with pytest.raises(ValueError):
            VectorRecord("memory-1", "model-v1", values)
    with pytest.raises(ValueError):
        VectorRecord("", "model-v1", (1.0,))
    with pytest.raises(ValueError):
        VectorQuery("model-v1", (1.0,), limit=0)
    with pytest.raises(ValueError):
        VectorMatch("memory-1", float("nan"))


def test_vector_values_are_immutable_and_canonical() -> None:
    values = [1, 0.5]
    record = VectorRecord("memory-1", "model-v1", values)
    values[0] = 99
    assert record.values == (1.0, 0.5)


def test_vector_index_can_be_implemented_without_an_engine() -> None:
    class FakeIndex:
        def __init__(self) -> None:
            self.records: dict[tuple[str, str], VectorRecord] = {}

        async def upsert(self, records: list[VectorRecord]) -> None:
            for record in records:
                self.records[record.space, record.memory_id] = record

        async def delete(self, memory_ids: list[str]) -> None:
            self.records = {
                key: record
                for key, record in self.records.items()
                if record.memory_id not in memory_ids
            }

        async def search(self, query: VectorQuery) -> list[VectorMatch]:
            matches = [
                VectorMatch(
                    record.memory_id,
                    sum(a * b for a, b in zip(query.values, record.values, strict=True)),
                )
                for record in self.records.values()
                if record.space == query.space
            ]
            return sorted(matches, key=lambda match: match.score, reverse=True)[: query.limit]

    async def exercise() -> None:
        index = FakeIndex()
        await index.upsert(
            [
                VectorRecord("a", "model-v1", (1.0, 0.0)),
                VectorRecord("b", "model-v1", (0.0, 1.0)),
                VectorRecord("c", "model-v2", (1.0, 0.0)),
            ]
        )
        matches = await index.search(VectorQuery("model-v1", (1, 0)))
        assert [match.memory_id for match in matches] == ["a", "b"]
        await index.delete(["a"])
        matches = await index.search(VectorQuery("model-v1", (1, 0)))
        assert [match.memory_id for match in matches] == ["b"]

    asyncio.run(exercise())
