"""Bounded, process-local conversation context for Phase 1."""

import asyncio
import re
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from backend.providers.base import ChatMessage


class ConversationNotFound(LookupError):
    """A conversation ID is not present in this store."""


class ConversationCapacityError(RuntimeError):
    """All conversation slots are occupied by active requests."""


TITLE_LENGTH = 40
UNTITLED = "無題の会話"
_LINE_BREAKS = re.compile("[\r\n\v\f\x85\u2028\u2029]")
# Control, format (zero-width and bidirectional controls), surrogate, private and unassigned.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def derive_title(text: str) -> str:
    """First non-empty line of a user message, cleaned and cut to 40 characters."""
    for line in _LINE_BREAKS.split(text):
        cleaned = "".join(
            " " if char.isspace() else char
            for char in line
            if unicodedata.category(char) not in _DROPPED_CATEGORIES or char.isspace()
        )
        cleaned = " ".join(cleaned.split())
        if cleaned:
            return cleaned[:TITLE_LENGTH].rstrip()
    return UNTITLED


def now_stamp() -> str:
    """UTC time in the same shape SQLite's strftime writes (`...T..:..:..mmmZ`)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class ConversationSummary:
    id: UUID
    title: str
    updated_at: str
    message_count: int


@dataclass(frozen=True)
class ConversationList:
    items: list[ConversationSummary]
    has_more: bool


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str


@dataclass(frozen=True)
class MessagePage:
    """Messages in chronological order; `has_more` says older ones exist before the first."""

    messages: list[StoredMessage]
    has_more: bool
    total: int = 0  # how many messages the conversation holds in all


@dataclass
class Conversation:
    messages: list[ChatMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_requests: int = 0
    # Bookkeeping for the process-local store only (the SQLite store reads these from disk).
    title: str = ""
    updated_at: str = ""
    dropped: int = 0


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

    async def list_conversations(
        self, *, limit: int, before: tuple[str, str] | None = None
    ) -> ConversationList:
        """Newest activity first; `before` is the (updated_at, id) of the last item seen."""
        async with self._lock:
            rows = [
                ConversationSummary(
                    cid,
                    c.title or UNTITLED,
                    c.updated_at,
                    c.dropped + len(c.messages),
                )
                for cid, c in self._conversations.items()
                if c.messages
            ]
        rows.sort(key=lambda r: (r.updated_at, str(r.id)), reverse=True)
        if before is not None:
            rows = [r for r in rows if (r.updated_at, str(r.id)) < before]
        return ConversationList(rows[:limit], len(rows) > limit)

    async def messages_page(
        self, conversation_id: UUID, *, limit: int, before: int | None = None
    ) -> MessagePage | None:
        """Up to `limit` messages older than message id `before`, or None when unknown."""
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return None
            stored = [
                StoredMessage(conversation.dropped + index, m.role, m.content)
                for index, m in enumerate(conversation.messages)
            ]
        all_count = conversation.dropped + len(stored)
        if before is not None:
            stored = [m for m in stored if m.id < before]
        return MessagePage(stored[-limit:], len(stored) > limit, all_count)

    async def remember(
        self, conversation_id: UUID, conversation: Conversation, user: str, assistant: str
    ) -> None:
        if not conversation.title and not conversation.dropped and not conversation.messages:
            conversation.title = derive_title(user)
        conversation.updated_at = now_stamp()
        conversation.messages.extend(
            (
                ChatMessage(role="user", content=user),
                ChatMessage(role="assistant", content=assistant),
            )
        )
        excess = len(conversation.messages) - self.max_messages
        if excess > 0:
            conversation.dropped += excess
            del conversation.messages[:excess]

    async def start_conversation(self, user_text: str) -> UUID:
        """A new conversation holding one user message and no answer yet (a background task will
        post the answers). Raises ``ConversationCapacityError`` when no slot is free."""
        text = clean_posted_text(user_text)
        async with self.open(None) as (conversation_id, conversation):
            conversation.title = derive_title(text)
            conversation.updated_at = now_stamp()
            conversation.messages.append(ChatMessage(role="user", content=text))
            return conversation_id

    async def append_assistant_message(self, conversation_id: UUID, text: str) -> None:
        """Add one assistant message to a known conversation (a finished background task's
        answer). It waits for a turn in progress, so it never lands in the middle of one.
        Raises ``ConversationNotFound`` for an unknown conversation."""
        text = clean_posted_text(text)
        async with self.open(conversation_id) as (current_id, conversation):
            await self._append(current_id, conversation, text)

    async def _append(self, conversation_id: UUID, conversation: Conversation, text: str) -> None:
        conversation.updated_at = now_stamp()
        conversation.messages.append(ChatMessage(role="assistant", content=text))
        excess = len(conversation.messages) - self.max_messages
        if excess > 0:
            conversation.dropped += excess
            del conversation.messages[:excess]

    async def messages_after(
        self, conversation_id: UUID, after: int, limit: int
    ) -> tuple[int, MessagePage] | None:
        """(total stored messages, up to `limit` messages after the first `after` ones), or
        None. `has_more` on the page says even newer ones exist beyond it."""
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return None
            stored = [
                StoredMessage(conversation.dropped + index, m.role, m.content)
                for index, m in enumerate(conversation.messages)
            ]
            total = conversation.dropped + len(stored)
        wanted = [m for m in stored if m.id >= after]
        return total, MessagePage(wanted[:limit], len(wanted) > limit, total)


#: Longest text a background task may post into a conversation.
MAX_POSTED_CHARS = 8000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean_posted_text(text: str) -> str:
    """Bounded text without control or bidi characters (newlines and tabs stay)."""
    cleaned = _CONTROL.sub(" ", text).strip()
    if not cleaned:
        raise ValueError("nothing to post")
    return cleaned if len(cleaned) <= MAX_POSTED_CHARS else cleaned[: MAX_POSTED_CHARS - 1] + "…"
