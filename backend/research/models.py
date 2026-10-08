"""Research session, source, and citation records.

These are plain, immutable values. Source text and page content are untrusted
data: only a digest of what was read is kept, never executed or interpreted.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

MAX_QUESTION_CHARS = 2000
MAX_QUERY_CHARS = 500
MAX_RESULT_CHARS = 50_000
MAX_CLAIM_CHARS = 2000
MAX_QUOTE_CHARS = 500
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 500
MAX_PUBLISHER_CHARS = 200


class ResearchLevel(StrEnum):
    MEMORY = "memory"
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"
    EXTENSIVE = "extensive"


class ResearchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class FailureReason(StrEnum):
    """Fixed short codes; upstream error messages are never stored."""

    SEARCH_FAILED = "search_failed"
    NO_RESULTS = "no_results"
    READER_FAILED = "reader_failed"
    SYNTHESIS_FAILED = "synthesis_failed"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERNAL_ERROR = "internal_error"


class SourceType(StrEnum):
    OFFICIAL = "official"
    DOCS = "docs"
    ACADEMIC = "academic"
    NEWS = "news"
    COMMUNITY = "community"
    BLOG = "blog"
    FORUM = "forum"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = frozenset(
    {ResearchStatus.FAILED, ResearchStatus.COMPLETED, ResearchStatus.CANCELLED}
)

ALLOWED_TRANSITIONS: dict[ResearchStatus, frozenset[ResearchStatus]] = {
    ResearchStatus.PENDING: frozenset({ResearchStatus.RUNNING, ResearchStatus.CANCELLED}),
    ResearchStatus.RUNNING: frozenset(
        {
            ResearchStatus.WAITING,
            ResearchStatus.COMPLETED,
            ResearchStatus.FAILED,
            ResearchStatus.CANCELLED,
        }
    ),
    ResearchStatus.WAITING: frozenset(
        {ResearchStatus.RUNNING, ResearchStatus.CANCELLED, ResearchStatus.FAILED}
    ),
    ResearchStatus.FAILED: frozenset(),
    ResearchStatus.COMPLETED: frozenset(),
    ResearchStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class SourceEvaluation:
    """Independent 0..1 ratings; None means not yet evaluated."""

    authority: float | None = None
    freshness: float | None = None
    primary: float | None = None
    relevance: float | None = None
    agreement: float | None = None

    def __post_init__(self) -> None:
        for name in ("authority", "freshness", "primary", "relevance", "agreement"):
            value = getattr(self, name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} rating must be a number from 0 to 1 or None")


@dataclass(frozen=True)
class ResearchSession:
    id: UUID
    question: str
    level: ResearchLevel
    status: ResearchStatus
    created_at: datetime
    updated_at: datetime
    result_text: str | None = None
    failure_reason: FailureReason | None = None


@dataclass(frozen=True)
class ResearchQueryRecord:
    id: UUID
    session_id: UUID
    text: str
    position: int
    created_at: datetime


@dataclass(frozen=True)
class ResearchSource:
    id: UUID
    session_id: UUID
    url: str
    final_url: str
    title: str | None
    publisher: str | None
    published_at: datetime | None
    retrieved_at: datetime
    content_digest: str
    source_type: SourceType = SourceType.UNKNOWN
    evaluation: SourceEvaluation = SourceEvaluation()


@dataclass(frozen=True)
class ResearchClaim:
    id: UUID
    session_id: UUID
    claim_text: str
    source_id: UUID
    quote: str
    quote_start: int | None = None
    quote_end: int | None = None
