"""Shared identity of the owner-approved automatic approval of research memory.

Owner decision (docs/memory.md, "Owner-approved exception"): research-origin candidates may be
approved by the system itself. The approval goes through the normal ``MemoryWriter.approve`` path
and is recorded in the review audit under the fixed actor below, so it can always be told apart
from a human review. Nothing here approves anything.
"""

from uuid import UUID

from backend.memory.repository import MemoryRepository, MemoryStatus

#: Review-audit actor of an automatic approval. Fixed text, never built from stored content.
AUTO_APPROVER = "auto:research"
#: Review-audit actor of a withdrawal pressed on the /memory screen.
WITHDRAW_ACTOR = "web:owner"
WITHDRAW_REASON = "withdrawn auto-approved research memory"
#: Short fixed labels placed in front of a research note's text in the model-facing context.
CONTEXT_LABEL_AUTO = "(調査由来・自動承認)"
CONTEXT_LABEL_RESEARCH = "(調査由来)"


def is_auto_approved(repository: MemoryRepository, memory_id: UUID) -> bool:
    """Whether the approval of this memory was recorded as automatic."""
    return any(
        event.action == "approve"
        and event.new_status is MemoryStatus.APPROVED
        and event.actor == AUTO_APPROVER
        for event in repository.review_events(memory_id)
    )
