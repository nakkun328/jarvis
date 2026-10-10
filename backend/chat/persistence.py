"""SQLite-backed conversation context with a bounded prompt window."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from backend.chat.context import (
    Conversation,
    ConversationCapacityError,
    ConversationNotFound,
    ConversationStore,
)
from backend.core.database import Database
from backend.providers.base import ChatMessage


class ConversationStorageError(RuntimeError):
    """The durable conversation store could not be read or updated."""


class SQLiteConversationStore(ConversationStore):
    """Keep complete successful turns on disk and only recent turns in the prompt."""

    def __init__(
        self, database: Database, *, max_conversations: int = 100, max_messages: int = 20
    ) -> None:
        super().__init__(max_conversations=max_conversations, max_messages=max_messages)
        self.database = database

    def _load(self, conversation_id: UUID) -> list[ChatMessage] | None:
        try:
            with self.database.connect(read_only=True) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                if exists is None:
                    return None
                rows = connection.execute(
                    "SELECT role, content FROM ("
                    "SELECT id, role, content FROM conversation_messages "
                    "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?"
                    ") ORDER BY id",
                    (str(conversation_id), self.max_messages),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise ConversationStorageError("Conversation storage unavailable") from exc
        return [ChatMessage(role=row["role"], content=row["content"]) for row in rows]

    async def history(self, conversation_id: UUID) -> list[ChatMessage] | None:
        """The last `max_messages` stored messages, or None when the conversation is unknown."""
        return await asyncio.to_thread(self._load, conversation_id)

    @asynccontextmanager
    async def open(self, conversation_id: UUID | None) -> AsyncIterator[tuple[UUID, Conversation]]:
        is_new = conversation_id is None
        async with self._lock:
            if is_new:
                conversation_id = uuid4()
            assert conversation_id is not None
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                if len(self._conversations) >= self.max_conversations:
                    for old_id, old_conversation in self._conversations.items():
                        if old_conversation.active_requests == 0:
                            del self._conversations[old_id]
                            break
                    else:
                        raise ConversationCapacityError("All conversation slots are busy")
                conversation = Conversation()
                self._conversations[conversation_id] = conversation
            else:
                self._conversations.move_to_end(conversation_id)
            conversation.active_requests += 1

        try:
            async with conversation.lock:
                if not is_new:
                    messages = await asyncio.to_thread(self._load, conversation_id)
                    if messages is None:
                        raise ConversationNotFound(str(conversation_id))
                    conversation.messages = messages
                yield conversation_id, conversation
        finally:
            async with self._lock:
                conversation.active_requests -= 1
                if not conversation.messages and conversation.active_requests == 0:
                    self._conversations.pop(conversation_id, None)

    def _save(self, conversation_id: UUID, user: str, assistant: str) -> None:
        try:
            with self.database.connect() as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    now = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                    connection.execute(
                        "INSERT OR IGNORE INTO conversations (id, created_at, updated_at) "
                        f"VALUES (?, {now}, {now})",
                        (str(conversation_id),),
                    )
                    connection.execute(
                        f"UPDATE conversations SET updated_at = {now} WHERE id = ?",
                        (str(conversation_id),),
                    )
                    connection.executemany(
                        "INSERT INTO conversation_messages "
                        f"(conversation_id, role, content, created_at) VALUES (?, ?, ?, {now})",
                        (
                            (str(conversation_id), "user", user),
                            (str(conversation_id), "assistant", assistant),
                        ),
                    )
        except (OSError, sqlite3.Error) as exc:
            raise ConversationStorageError("Conversation storage unavailable") from exc

    async def remember(
        self, conversation_id: UUID, conversation: Conversation, user: str, assistant: str
    ) -> None:
        await asyncio.to_thread(self._save, conversation_id, user, assistant)
        await super().remember(conversation_id, conversation, user, assistant)
