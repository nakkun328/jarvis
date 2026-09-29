"""Provenance and score invariants for memory records."""

from dataclasses import replace

import pytest

from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord


def test_memory_record_distinguishes_explicit_fact_from_inference() -> None:
    explicit = MemoryRecord(
        category=MemoryCategory.USER,
        content="Prefers concise replies",
        source="conversation:123",
        origin=MemoryOrigin.USER_EXPLICIT,
        importance=0.8,
        confidence=1.0,
        tags=("communication",),
    )
    inferred = replace(explicit, origin=MemoryOrigin.AI_INFERENCE, confidence=0.4)
    assert not explicit.is_inference
    assert inferred.is_inference
    assert explicit.id == inferred.id


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf")])
def test_rejects_invalid_confidence(score: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        MemoryRecord(
            category=MemoryCategory.SELF,
            content="Retry failed once",
            source="tool-run:1",
            origin=MemoryOrigin.TOOL_OBSERVATION,
            importance=0.5,
            confidence=score,
        )
