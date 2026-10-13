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


# The only values ``ProviderError.status`` can take: a coarse reason a request failed, safe to log.
STATUS_BUCKETS = frozenset(
    {"401", "403", "404", "413", "429", "4xx", "5xx", "timeout", "network", "other"}
)


def status_bucket(exc: BaseException) -> str:
    """Classify a failed provider request without keeping any upstream text.

    Works for httpx errors (``response.status_code``) and the OpenAI SDK's ``APIError`` family
    (``status_code``); timeouts and connection failures are recognised by class name.
    """
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "Connect" in name or "Network" in name:
        return "network"
    code = getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    if not isinstance(code, int) or isinstance(code, bool):
        return "other"
    if str(code) in STATUS_BUCKETS:
        return str(code)
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


class ProviderError(RuntimeError):
    """A provider failed to fulfill a request.

    ``status`` is an optional coarse bucket from ``STATUS_BUCKETS`` (never upstream text).
    """

    def __init__(self, message: str = "", *, status: str | None = None) -> None:
        super().__init__(message)
        self.status = status if status in STATUS_BUCKETS else None


class LLMProvider(Protocol):
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]: ...
