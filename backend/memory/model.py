"""Shared, storage-neutral memory record and provenance."""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class MemoryCategory(StrEnum):
    USER = "user"
    PROJECT = "project"
    CONVERSATION = "conversation"
    WORK_STATE = "work_state"
    TEMPORARY = "temporary"
    SELF = "self"


class MemoryOrigin(StrEnum):
    USER_EXPLICIT = "user_explicit"
    AI_INFERENCE = "ai_inference"
    TOOL_OBSERVATION = "tool_observation"


@dataclass(frozen=True)
class MemoryRecord:
    category: MemoryCategory
    content: str
    source: str
    origin: MemoryOrigin
    importance: float
    confidence: float
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_accessed: datetime | None = None
    tags: tuple[str, ...] = ()
    project: str | None = None

    def __post_init__(self) -> None:
        if not self.content.strip() or not self.source.strip():
            raise ValueError("Memory content and source must contain text")
        if not isinstance(self.category, MemoryCategory) or not isinstance(
            self.origin, MemoryOrigin
        ):
            raise ValueError("Memory category and origin must use defined values")
        for name, value in (("importance", self.importance), ("confidence", self.confidence)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
                or not math.isfinite(value)
            ):
                raise ValueError(f"Memory {name} must be between 0 and 1")
        for name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("last_accessed", self.last_accessed),
        ):
            if value is not None and (
                not isinstance(value, datetime) or value.utcoffset() is None
            ):
                raise ValueError(f"Memory {name} must have a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("Memory updated_at must not precede created_at")
        if self.last_accessed is not None and self.last_accessed < self.created_at:
            raise ValueError("Memory last_accessed must not precede created_at")
        if any(not tag.strip() for tag in self.tags) or len(set(self.tags)) != len(self.tags):
            raise ValueError("Memory tags must be nonblank and unique")
        if self.project is not None and not self.project.strip():
            raise ValueError("Memory project must contain text")

    @property
    def is_inference(self) -> bool:
        return self.origin is MemoryOrigin.AI_INFERENCE
