"""Stage reviewed lessons about the assistant's own behavior."""

from enum import StrEnum
from uuid import UUID

from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.repository import StoredMemory
from backend.memory.writer import MemoryWriter


class SelfMemoryKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CORRECTION = "correction"


_FIELDS = {
    SelfMemoryKind.SUCCESS: ("Succeeded", "Repeat"),
    SelfMemoryKind.FAILURE: ("Failed", "Next time"),
    SelfMemoryKind.CORRECTION: ("Correction", "Use instead"),
}
_KIND_TAG_PREFIX = "self-kind:"


class SelfMemoryRecorder:
    """Record an observed result and a concrete lesson as a pending candidate.

    The caller must provide provenance and scores. Review and publication remain
    separate operations on ``MemoryWriter``; recording never approves a lesson.
    """

    def __init__(self, writer: MemoryWriter) -> None:
        self.writer = writer

    def record(
        self,
        *,
        kind: SelfMemoryKind,
        observation: str,
        guidance: str,
        source: str,
        origin: MemoryOrigin,
        importance: float,
        confidence: float,
        tags: tuple[str, ...] = (),
        project: str | None = None,
        supersedes_id: UUID | None = None,
    ) -> StoredMemory:
        """Stage a success, failure, or correction without changing earlier memories."""
        if not isinstance(kind, SelfMemoryKind):
            raise ValueError("kind must be a SelfMemoryKind")
        if not observation.strip() or not guidance.strip():
            raise ValueError("Self memory observation and guidance must contain text")
        if any(tag.startswith(_KIND_TAG_PREFIX) for tag in tags):
            raise ValueError("Self memory kind tags are reserved")
        if supersedes_id is not None and kind is not SelfMemoryKind.CORRECTION:
            raise ValueError("Only correction lessons may supersede an earlier memory")

        observation_label, guidance_label = _FIELDS[kind]
        record = MemoryRecord(
            category=MemoryCategory.SELF,
            content=(
                f"{observation_label}: {observation.strip()}\n{guidance_label}: {guidance.strip()}"
            ),
            source=source,
            origin=origin,
            importance=importance,
            confidence=confidence,
            tags=(_KIND_TAG_PREFIX + kind.value, *tags),
            project=project,
        )
        if supersedes_id is not None:
            return self.writer.submit_correction(supersedes_id, record)
        return self.writer.submit(record)
