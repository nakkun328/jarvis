"""Vendor-neutral LLM provider contract."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    provider: str
    model: str


class ProviderError(RuntimeError):
    """A provider failed to fulfill a request."""


class LLMProvider(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]: ...
