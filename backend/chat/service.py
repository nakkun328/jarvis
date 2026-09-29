"""Vendor-neutral chat flow with context updates after verified responses."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from backend.chat.context import ConversationStore
from backend.personality.prompt import SYSTEM_PROMPT
from backend.providers.base import (
    ChatMessage,
    CompletionRequest,
    LLMProvider,
    ProviderError,
)


@dataclass(frozen=True)
class ChatResult:
    conversation_id: UUID
    reply: str
    provider: str
    model: str


@dataclass(frozen=True)
class ChatDelta:
    text: str


@dataclass(frozen=True)
class ChatDone:
    conversation_id: UUID
    provider: str
    model: str


class ChatService:
    def __init__(self, provider: LLMProvider, store: ConversationStore | None = None) -> None:
        self.provider = provider
        self.store = store or ConversationStore()

    @staticmethod
    def _request(history: list[ChatMessage], message: str) -> CompletionRequest:
        return CompletionRequest(
            messages=(
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                *history,
                ChatMessage(role="user", content=message),
            )
        )

    async def complete(self, message: str, conversation_id: UUID | None = None) -> ChatResult:
        async with self.store.open(conversation_id) as (current_id, conversation):
            response = await self.provider.complete(self._request(conversation.messages, message))
            if not response.text.strip():
                raise ProviderError("Provider returned no text")
            self.store.remember(conversation, message, response.text)
            return ChatResult(current_id, response.text, response.provider, response.model)

    async def stream(
        self, message: str, conversation_id: UUID | None = None
    ) -> AsyncIterator[ChatDelta | ChatDone]:
        async with self.store.open(conversation_id) as (current_id, conversation):
            chunks: list[str] = []
            async for delta in self.provider.stream(self._request(conversation.messages, message)):
                if delta:
                    chunks.append(delta)
                    yield ChatDelta(delta)
            reply = "".join(chunks)
            if not reply.strip():
                raise ProviderError("Provider returned no text")
            self.store.remember(conversation, message, reply)
            yield ChatDone(
                conversation_id=current_id,
                provider=str(getattr(self.provider, "name", "custom")),
                model=str(getattr(self.provider, "model", "unknown")),
            )
