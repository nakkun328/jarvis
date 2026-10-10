"""Research session, source, and citation records.

These are plain, immutable values. Source text and page content are untrusted
data: only a digest of what was read is kept, never executed or interpreted.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
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


class Basis(StrEnum):
    """What a source type decision rested on, strongest first."""

    HOST = "host"
    PATH = "path"
    TITLE = "title"
    DEFAULT = "default"
    PROVIDED = "provided"  # the caller supplied the type; nothing was inferred


class RatingName(StrEnum):
    """The ratings that carry stored reason codes."""

    AUTHORITY = "authority"
    FRESHNESS = "freshness"
    RELEVANCE = "relevance"
    AGREEMENT = "agreement"


class RatingReason(StrEnum):
    """Fixed codes explaining each rating. No text from a page or question is ever a code."""

    AUTHORITY_BY_TYPE = "authority_by_type"
    AUTHORITY_CAPPED_WEAK_BASIS = "authority_capped_weak_basis"
    AUTHORITY_UNCLASSIFIED = "authority_unclassified"
    AUTHORITY_SUBJECT_OFFICIAL = "authority_subject_official"
    FRESHNESS_DECAY = "freshness_decay"
    FRESHNESS_UNKNOWN_DATE = "freshness_unknown_date"
    FRESHNESS_FUTURE_DATE = "freshness_future_date"
    RELEVANCE_OVERLAP = "relevance_overlap"
    RELEVANCE_TITLE_ONLY = "relevance_title_only"
    RELEVANCE_NO_TERMS = "relevance_no_terms"
    RELEVANCE_NO_TEXT = "relevance_no_text"
    AGREEMENT_CORROBORATED = "agreement_corroborated"
    AGREEMENT_MIXED = "agreement_mixed"
    AGREEMENT_CONTRADICTED = "agreement_contradicted"
    AGREEMENT_NO_COMPARISON = "agreement_no_comparison"
    AGREEMENT_VERBATIM_SUPPORT = "agreement_verbatim_support"
    AGREEMENT_NUMBER_MISMATCH = "agreement_number_mismatch"
    AGREEMENT_DATE_MISMATCH = "agreement_date_mismatch"
    AGREEMENT_NEGATION_MISMATCH = "agreement_negation_mismatch"


#: Which reason codes may explain which rating (checked on every write).
RATING_REASONS: Mapping[RatingName, frozenset[RatingReason]] = MappingProxyType(
    {
        RatingName.AUTHORITY: frozenset(
            {
                RatingReason.AUTHORITY_BY_TYPE,
                RatingReason.AUTHORITY_CAPPED_WEAK_BASIS,
                RatingReason.AUTHORITY_UNCLASSIFIED,
                RatingReason.AUTHORITY_SUBJECT_OFFICIAL,
            }
        ),
        RatingName.FRESHNESS: frozenset(
            {
                RatingReason.FRESHNESS_DECAY,
                RatingReason.FRESHNESS_UNKNOWN_DATE,
                RatingReason.FRESHNESS_FUTURE_DATE,
            }
        ),
        RatingName.RELEVANCE: frozenset(
            {
                RatingReason.RELEVANCE_OVERLAP,
                RatingReason.RELEVANCE_TITLE_ONLY,
                RatingReason.RELEVANCE_NO_TERMS,
                RatingReason.RELEVANCE_NO_TEXT,
            }
        ),
        RatingName.AGREEMENT: frozenset(
            {
                RatingReason.AGREEMENT_CORROBORATED,
                RatingReason.AGREEMENT_MIXED,
                RatingReason.AGREEMENT_CONTRADICTED,
                RatingReason.AGREEMENT_NO_COMPARISON,
                RatingReason.AGREEMENT_VERBATIM_SUPPORT,
                RatingReason.AGREEMENT_NUMBER_MISMATCH,
                RatingReason.AGREEMENT_DATE_MISMATCH,
                RatingReason.AGREEMENT_NEGATION_MISMATCH,
            }
        ),
    }
)
MAX_REASONS_PER_RATING = 4
MAX_RULE_ID_CHARS = 64


class ConflictKind(StrEnum):
    """How two statements disagree. The vocabulary is fixed; no text is stored."""

    NUMBER_MISMATCH = "number_mismatch"
    DATE_MISMATCH = "date_mismatch"
    NEGATION_MISMATCH = "negation_mismatch"


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ConflictResolution(StrEnum):
    """How a person closed a conflict. Nothing in the library sets these by itself."""

    BOTH_REPORTED = "both_reported"  # the disagreement is kept and shown to the reader
    FIRST_PREFERRED = "first_preferred"
    SECOND_PREFERRED = "second_preferred"
    NOT_A_CONFLICT = "not_a_conflict"  # false positive of the heuristic


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
class RatingReasons:
    """Fixed reason codes per rating, in the order they were recorded (empty: none stored)."""

    authority: tuple[RatingReason, ...] = ()
    freshness: tuple[RatingReason, ...] = ()
    relevance: tuple[RatingReason, ...] = ()
    agreement: tuple[RatingReason, ...] = ()

    def get(self, name: RatingName) -> tuple[RatingReason, ...]:
        return getattr(self, RatingName(name).value)


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
    #: Past-research reuse decision (a ``reuse.ReuseReason`` value), the earlier session it
    #: concerns, and the retrieval date of that session's oldest source. All ``None`` when
    #: no decision was recorded.
    reuse_reason: str | None = None
    reuse_of: UUID | None = None
    reuse_prior_at: datetime | None = None


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
    # How the type was decided; None for rows stored before classification was recorded.
    classification_rule: str | None = None
    classification_basis: Basis | None = None
    reasons: RatingReasons = RatingReasons()


@dataclass(frozen=True)
class ResearchClaim:
    id: UUID
    session_id: UUID
    claim_text: str
    source_id: UUID
    quote: str
    quote_start: int | None = None
    quote_end: int | None = None


@dataclass(frozen=True)
class ResearchConflict:
    """Two statements that disagree, linked to claims and sources by id only.

    Side A is always a claim (``claim_a_id``) and the source it cites. Side B is either a
    second claim (``claim_b_id`` set) or a source whose text disagrees with claim A
    (``claim_b_id`` is None). The record holds no text; it stays ``open`` until a person
    resolves it.
    """

    id: UUID
    session_id: UUID
    kind: ConflictKind
    claim_a_id: UUID
    source_a_id: UUID
    source_b_id: UUID
    detected_at: datetime
    claim_b_id: UUID | None = None
    status: ConflictStatus = ConflictStatus.OPEN
    resolution: ConflictResolution | None = None
    resolved_at: datetime | None = None
