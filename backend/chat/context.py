"""Bounded, process-local conversation context for Phase 1."""

import asyncio
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from backend.providers.base import ChatMessage


class ConversationNotFound(LookupError):
    """A conversation ID is not present in this store."""


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

    async def history(self, conversation_id: UUID) -> list[ChatMessage] | None:
        """Read-only copy of the retained messages, or None when the conversation is unknown."""
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
        return None if conversation is None else list(conversation.messages)

    async def remember(
        self, conversation_id: UUID, conversation: Conversation, user: str, assistant: str
    ) -> None:
        conversation.messages.extend(
            (
                ChatMessage(role="user", content=user),
                ChatMessage(role="assistant", content=assistant),
            )
        )
        del conversation.messages[: -self.max_messages]

    async def append_assistant_message(self, conversation_id: UUID, text: str) -> None:
        """Add one assistant message to a known conversation (a finished background task's
        answer). It waits for a turn in progress, so it never lands in the middle of one.
        Raises ``ConversationNotFound`` for an unknown conversation."""
        text = clean_posted_text(text)
        async with self.open(conversation_id) as (current_id, conversation):
            await self._append(current_id, conversation, text)

    async def _append(self, conversation_id: UUID, conversation: Conversation, text: str) -> None:
        conversation.messages.append(ChatMessage(role="assistant", content=text))
        del conversation.messages[: -self.max_messages]

    async def messages_after(
        self, conversation_id: UUID, after: int
    ) -> tuple[int, list[ChatMessage]] | None:
        """(total stored messages, the messages after the first `after` ones), or None."""
        messages = await self.history(conversation_id)
        if messages is None:
            return None
        return len(messages), messages[after:]


#: Longest text a background task may post into a conversation.
MAX_POSTED_CHARS = 8000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean_posted_text(text: str) -> str:
    """Bounded text without control or bidi characters (newlines and tabs stay)."""
    cleaned = _CONTROL.sub(" ", text).strip()
    if not cleaned:
        raise ValueError("nothing to post")
    return cleaned if len(cleaned) <= MAX_POSTED_CHARS else cleaned[: MAX_POSTED_CHARS - 1] + "…"
