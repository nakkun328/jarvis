"""Starting a research from a chat turn: a small port, and an adapter over the run service.

``ResearchStarter`` is all the chat needs from the research side: hand over ONE question and learn
a fixed outcome. The adapter below is backed by ``ResearchRuns`` (the same service the Research
screen's ``POST /api/research/sessions`` uses), so there is no second execution path: the
one-research-at-a-time rule, the local search budget and the question rules are the service's.

What is handed over is the text it is given and nothing else: the chat passes the user's message
only, never memory notes or history. Outcomes are fixed codes; no outcome carries the question, a
query, a URL or any vendor text, and the adapter logs only a fixed event name and an error type.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from backend.research.models import ResearchLevel
from backend.research.run_control import (
    REQUESTABLE_LEVELS,
    BudgetExhausted,
    Busy,
    ResearchRuns,
    question_is_valid,
)

_LOG = logging.getLogger(__name__)

#: Levels a chat turn may start, as the ``JARVIS_CHAT_RESEARCH_LEVEL`` values. Quick is the
#: default because it is the cheaper one.
CHAT_RESEARCH_LEVELS = tuple(level.value for level in REQUESTABLE_LEVELS)
DEFAULT_CHAT_RESEARCH_LEVEL = ResearchLevel.QUICK.value


class StartKind(StrEnum):
    STARTED = "started"
    BUSY = "busy"
    NOT_CONFIGURED = "not_configured"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REFUSED = "refused"


@dataclass(frozen=True)
class StartOutcome:
    kind: StartKind
    session_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StartKind):
            raise ValueError("unknown start outcome")
        if (self.kind is StartKind.STARTED) != isinstance(self.session_id, UUID):
            raise ValueError("a session id belongs to a started research, and only to it")

    @classmethod
    def started(cls, session_id: UUID) -> "StartOutcome":
        return cls(StartKind.STARTED, session_id)


class ResearchStarter(Protocol):
    async def start(self, question: str) -> StartOutcome:
        """Start a research of ``question``. Returns a fixed outcome; does not raise, except that
        cancellation propagates."""
        ...


class RunServiceStarter:
    """A starter over the research run service, at one fixed level.

    ``runs=None`` is a starter whose research is not available (always ``not_configured``).
    """

    def __init__(
        self, runs: ResearchRuns | None, level: ResearchLevel = ResearchLevel.QUICK
    ) -> None:
        if level not in REQUESTABLE_LEVELS:
            raise ValueError("this research level is not available")
        self._runs = runs
        self._level = level

    async def start(self, question: str) -> StartOutcome:
        if self._runs is None:
            return StartOutcome(StartKind.NOT_CONFIGURED)
        if not question_is_valid(question):
            return StartOutcome(StartKind.REFUSED)
        try:
            # ``submit`` is blocking (a lock and SQLite), so it runs off the event loop. A
            # cancellation that arrives while it runs cannot undo it: a research it queued stays
            # visible on the Research screen, where it can be cancelled.
            session_id = await asyncio.to_thread(self._runs.submit, question, self._level)
        except Busy:
            return StartOutcome(StartKind.BUSY)
        except BudgetExhausted:
            return StartOutcome(StartKind.BUDGET_EXHAUSTED)
        except Exception as exc:  # CancelledError is a BaseException and propagates.
            _LOG.warning("chat.research_start_failed", extra={"error_type": type(exc).__name__})
            return StartOutcome(StartKind.REFUSED)
        return StartOutcome.started(session_id)
