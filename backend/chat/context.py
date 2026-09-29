"""Bounded, process-local conversation context for Phase 1."""

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from backend.providers.base import ChatMessage


class ConversationNotFound(LookupError):
    """A conversation ID has expired or belongs to another process."""


class ConversationCapacityError(RuntimeError):
    """All conversation slots are occupied by active requests."""


@dataclass
class Conversation:
    messages: list[ChatMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_requests: int = 0


class ConversationStore:
    def __init__(self, *, max_conversations: int = 100, max_messages: int = 20) -> None:
        if max_conversations < 1 or max_messages < 2 or max_messages % 2:
            raise ValueError("Conversation limits must be positive, with an even message limit")
        self.max_conversations = max_conversations
        self.max_messages = max_messages
        self._conversations: OrderedDict[UUID, Conversation] = OrderedDict()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def open(self, conversation_id: UUID | None) -> AsyncIterator[tuple[UUID, Conversation]]:
        async with self._lock:
            if conversation_id is None:
                if len(self._conversations) >= self.max_conversations:
                    for old_id, old_conversation in self._conversations.items():
                        if old_conversation.active_requests == 0:
                            del self._conversations[old_id]
                            break
                    else:
                        raise ConversationCapacityError("All conversation slots are busy")
                conversation_id = uuid4()
                conversation = Conversation()
                self._conversations[conversation_id] = conversation
            else:
                conversation = self._conversations.get(conversation_id)
                if conversation is None:
                    raise ConversationNotFound(str(conversation_id))
                self._conversations.move_to_end(conversation_id)
            conversation.active_requests += 1

        try:
            async with conversation.lock:
                yield conversation_id, conversation
        finally:
            async with self._lock:
                conversation.active_requests -= 1

    def remember(self, conversation: Conversation, user: str, assistant: str) -> None:
        conversation.messages.extend(
            (
                ChatMessage(role="user", content=user),
                ChatMessage(role="assistant", content=assistant),
            )
        )
        del conversation.messages[: -self.max_messages]
