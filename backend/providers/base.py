"""Provider-neutral text generation contract."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "developer", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("Invalid message role")
        if not self.content.strip():
            raise ValueError("Message content must not be empty")


@dataclass(frozen=True)
class Generation:
    text: str
    provider: str
    model: str


class ProviderError(Exception):
    """A provider request failed without exposing provider or secret details."""


@runtime_checkable
class LLMProvider(Protocol):
    def generate(self, messages: Sequence[Message]) -> Generation: ...
