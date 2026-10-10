"""What the HTTP layer may ask of the research run service, without importing the pipeline.

The API module imports only this file, so the application starts without the HTTP client
library that the page reader needs. Every refusal carries a fixed code; no refusal ever
carries the question, a query, a URL or vendor text.
"""

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from backend.research.models import MAX_QUESTION_CHARS, ResearchLevel

#: Levels a person may request. Extensive research is not available. Deep is only ever chosen
#: by a human (the Research screen or ``JARVIS_CHAT_RESEARCH_LEVEL=deep``); nothing selects it
#: automatically.
REQUESTABLE_LEVELS: tuple[ResearchLevel, ...] = (
    ResearchLevel.QUICK,
    ResearchLevel.STANDARD,
    ResearchLevel.DEEP,
)

#: Fixed codes of `/api/research/status` `reason` when research cannot be started.
REASON_DISABLED = "disabled"
REASON_NO_SEARCH_PROVIDER = "no_search_provider"
REASON_NO_CHAT_PROVIDER = "no_chat_provider"
REASONS = frozenset({REASON_DISABLED, REASON_NO_SEARCH_PROVIDER, REASON_NO_CHAT_PROVIDER})


def question_is_valid(value: object) -> bool:
    """True when ``value`` is a question the run service accepts (the HTTP endpoint's rules)."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUESTION_CHARS:
        return False
    return not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value)


class RunRefused(Exception):
    """A request was refused for a fixed reason."""

    code = "refused"
    status_code = 409


class Busy(RunRefused):
    code = "busy"
    status_code = 429


class BudgetExhausted(RunRefused):
    code = "search_budget_exhausted"
    status_code = 429


class UnknownSession(RunRefused):
    code = "session_not_found"
    status_code = 404


class NotCancellable(RunRefused):
    code = "not_cancellable"
    status_code = 409


class ResearchRuns(Protocol):
    def submit(self, question: str, level: ResearchLevel) -> UUID:
        """Queue a research. Raises ``Busy`` or ``BudgetExhausted`` before anything is queued."""
        ...

    def cancel(self, session_id: UUID) -> str:
        """``"cancelled"`` when it stopped at once, ``"cancelling"`` when a run was signalled.

        Raises ``UnknownSession`` or ``NotCancellable``.
        """
        ...

    def progress(self, session_id: UUID) -> Mapping[str, object] | None:
        """Live counters of a run in this process, or ``None``."""
        ...
