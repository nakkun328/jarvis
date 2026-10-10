"""Shared identity of the owner-approved automatic approval of memory.

Owner decisions (docs/memory.md, "Owner-approved exceptions"): research-origin and chat-origin
candidates may be approved by the system itself. The approval goes through the normal
``MemoryWriter.approve`` path and is recorded in the review audit under a fixed actor, so it can
always be told apart from a human review. Nothing here approves anything.
"""

from uuid import UUID

from backend.memory.model import MemoryOrigin
from backend.memory.repository import MemoryRepository, MemoryStatus

#: Review-audit actor of an automatic approval. Fixed text, never built from stored content.
AUTO_APPROVER = "auto:research"
#: Review-audit actor of an automatic approval of a fact extracted from chat.
CHAT_AUTO_APPROVER = "auto:chat"
#: The automatic actor that may approve a note of each origin.
AUTO_ACTORS: dict[MemoryOrigin, str] = {
    MemoryOrigin.RESEARCH: AUTO_APPROVER,
    MemoryOrigin.CHAT: CHAT_AUTO_APPROVER,
}
#: Review-audit actor of a withdrawal pressed on the /memory screen.
WITHDRAW_ACTOR = "web:owner"
WITHDRAW_REASON = "withdrawn auto-approved research memory"
WITHDRAW_REASON_CHAT = "withdrawn auto-approved chat memory"
#: Short fixed labels placed in front of a note's text in the model-facing context.
CONTEXT_LABEL_AUTO = "(調査由来・自動承認)"
CONTEXT_LABEL_RESEARCH = "(調査由来)"
CONTEXT_LABEL_CHAT_AUTO = "(会話由来・自動承認)"
CONTEXT_LABEL_CHAT = "(会話由来)"


def is_auto_approved(
    repository: MemoryRepository,
    memory_id: UUID,
    origin: MemoryOrigin = MemoryOrigin.RESEARCH,
) -> bool:
    """Whether the approval of this memory was recorded as automatic for its origin."""
    actor = AUTO_ACTORS.get(origin)
    if actor is None:
        return False
    return any(
        event.action == "approve"
        and event.new_status is MemoryStatus.APPROVED
        and event.actor == actor
        for event in repository.review_events(memory_id)
    )
