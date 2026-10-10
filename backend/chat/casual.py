"""The casual path: a text-only answer to small talk (docs/casual-path.md).

It knows two things only: the personality settings and the last few turns of the CURRENT
conversation. It never receives approved memory, a summary, a tool or a research, and nothing
here can reach them: this module imports none of those. Every failure before the first token
ends as ``None`` so ``ChatService`` can run the Main Agent path instead; nothing is invented.
"""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from backend.core.config import DEFAULT_CASUAL_DAILY_CALL_LIMIT
from backend.personality.prompt import SYSTEM_PROMPT
from backend.providers.base import ChatMessage, CompletionRequest, CompletionResponse, LLMProvider

_LOG = logging.getLogger(__name__)

# One turn is a user message plus the assistant reply.
CASUAL_HISTORY_TURNS = 6
# The providers have no output-token option, so the limit is applied to the text instead: the
# stream is cut (and the model call closed) at this many characters. About 300 tokens.
CASUAL_MAX_OUTPUT_CHARS = 600

# Appended to the personality prompt. Fixed text; no setting can change it.
CASUAL_RULES = (
    "This is a short, casual chat. Reply briefly, in two or three sentences at most.\n"
    "You know nothing about the user beyond this conversation: no saved notes, no earlier "
    "conversations, no files, no web. Never claim to remember, look up or have done anything "
    "else. If the user needs any of that, say you would need to check your notes or look into "
    "it and ask them to ask again.\n"
)


def _today() -> date:
    return datetime.now(UTC).date()


class CasualService:
    """Builds the casual request, streams its answer and counts the day's calls."""

    def __init__(
        self,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        daily_call_limit: int = DEFAULT_CASUAL_DAILY_CALL_LIMIT,
        history_turns: int = CASUAL_HISTORY_TURNS,
        max_output_chars: int = CASUAL_MAX_OUTPUT_CHARS,
        today: Callable[[], date] = _today,
    ) -> None:
        if daily_call_limit < 1 or history_turns < 1 or max_output_chars < 1:
            raise ValueError("casual limits must be positive")
        self._prompt = system_prompt + CASUAL_RULES
        self.daily_call_limit = daily_call_limit
        self.history_turns = history_turns
        self.max_output_chars = max_output_chars
        self._today = today
        # The call counter lives in this process only: it is not stored and starts again from
        # zero on a restart. The day is the UTC date.
        self._day = today()
        self._calls = 0

    def reserve(self) -> bool:
        """Count one model call for today. False (and nothing counted) at the daily cap."""
        day = self._today()
        if day != self._day:
            self._day, self._calls = day, 0
        if self._calls >= self.daily_call_limit:
            return False
        self._calls += 1
        return True

    def request(self, history: list[ChatMessage], message: str) -> CompletionRequest:
        """The personality prompt, the last turns of this conversation and the new message."""
        recent = history[-2 * self.history_turns :]
        return CompletionRequest(
            messages=(
                ChatMessage(role="system", content=self._prompt),
                *recent,
                ChatMessage(role="user", content=message),
            )
        )

    async def complete(
        self, provider: LLMProvider, request: CompletionRequest
    ) -> CompletionResponse | None:
        """The whole reply, or ``None`` when the provider failed or returned no text."""
        try:
            response = await provider.complete(request)
        except Exception as exc:  # CancelledError is a BaseException and propagates.
            _LOG.warning("chat.casual_failed", extra={"error_type": type(exc).__name__})
            return None
        text = response.text[: self.max_output_chars]
        if not text.strip():
            _LOG.warning("chat.casual_failed", extra={"error_type": "EmptyReply"})
            return None
        return replace(response, text=text)

    async def start(
        self, provider: LLMProvider, request: CompletionRequest
    ) -> "CasualStream | None":
        """Open the provider stream and wait for the first token.

        ``None`` means it failed or said nothing before any text existed: the caller runs the
        Main Agent path. A failure after the first token surfaces from ``CasualStream.deltas``.
        """
        deltas = provider.stream(request)
        try:
            async for first in deltas:
                if first:
                    return CasualStream(first, deltas, self.max_output_chars)
        except Exception as exc:  # CancelledError is a BaseException and propagates.
            _LOG.warning("chat.casual_failed", extra={"error_type": type(exc).__name__})
        else:
            _LOG.warning("chat.casual_failed", extra={"error_type": "EmptyReply"})
        await _close(deltas)
        return None


async def _close(deltas: AsyncIterator[str]) -> None:
    close = getattr(deltas, "aclose", None)
    if close is not None:
        await close()


@dataclass
class CasualStream:
    first: str
    _rest: AsyncIterator[str]
    _limit: int

    async def deltas(self) -> AsyncIterator[str]:
        """The reply, cut at the character limit (the model call is closed there). A provider
        failure after the first token raises, as it does on the Main Agent path."""
        sent = 0
        try:
            pending: str | None = self.first
            while pending is not None:
                room = self._limit - sent
                piece = pending[:room]
                if piece:
                    sent += len(piece)
                    yield piece
                if len(pending) >= room:
                    return
                pending = None
                async for delta in self._rest:
                    if delta:
                        pending = delta
                        break
        finally:
            await _close(self._rest)
