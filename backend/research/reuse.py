"""Past-research reuse and its freshness policy (JAR-56, JAR-57).

Before a research searches, ``decide_reuse`` looks for an earlier research that

- is ``completed`` and has at least one stored claim (a claim is stored only after its quote
  was verified against its source; a result that only says "no claim could be verified" has no
  claims, and failed, cancelled or unfinished sessions are never candidates),
- asked a similar question (see ``question_similarity``), and
- was not made at a level lower than the one requested.

The decision is pure: no network, no model, no clock of its own (the caller passes ``now``).
It returns one of four fixed ``ReuseReason`` codes.

Freshness policy. The question is classed by a fixed vocabulary (``TOPIC_VOCABULARY``):

- ``time_sensitive`` (price, latest, news, today, weather, ...): the maximum age is zero. A
  prior result is never reused; the run searches again (``time_sensitive_topic``) and the
  prior session is only linked as "previous result" context.
- ``stable`` (everything else): the prior result may be reused while its age is at most
  ``DEFAULT_MAX_AGE_DAYS`` (a proposal, not a measured value). Older: search again
  (``prior_stale``).

The age of a prior result is the age of its oldest source (``retrieved_at``): a result is only
as fresh as its stalest citation. A date in the future counts as stale.

A reused answer keeps its original verified claims and sources (copied with their original
``retrieved_at``) and is shown with that date; it is never presented as freshly retrieved.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from backend.research.models import ResearchLevel, ResearchSession, ResearchSource, ResearchStatus
from backend.research.repository import ResearchRepository

#: Maximum age of a reusable prior result for a stable topic. Proposal, tunable.
DEFAULT_MAX_AGE_DAYS = 30
#: Minimum question similarity (0..1) for a prior research to be a candidate. Proposal.
DEFAULT_MIN_SIMILARITY = 0.8
#: How many of the newest completed sessions are examined.
CANDIDATE_SCAN_LIMIT = 200

_LEVEL_RANK = {
    ResearchLevel.MEMORY: 0,
    ResearchLevel.QUICK: 1,
    ResearchLevel.STANDARD: 2,
    ResearchLevel.DEEP: 3,
    ResearchLevel.EXTENSIVE: 4,
}


class TopicClass(StrEnum):
    TIME_SENSITIVE = "time_sensitive"
    STABLE = "stable"


class ReuseReason(StrEnum):
    """Fixed codes: what was decided about past research. Stored on the session."""

    REUSED_FRESH = "reused_fresh"
    NO_PRIOR_RESEARCH = "no_prior_research"
    PRIOR_STALE = "prior_stale"
    TIME_SENSITIVE_TOPIC = "time_sensitive_topic"


#: Words that make a topic time-sensitive. Matched on the normalised question (NFKC, lower).
TOPIC_VOCABULARY: tuple[str, ...] = (
    # Japanese
    "価格",
    "値段",
    "料金",
    "相場",
    "株価",
    "為替",
    "レート",
    "最新",
    "最近",
    "現在",
    "今日",
    "今週",
    "今月",
    "今年",
    "ニュース",
    "速報",
    "天気",
    "天候",
    "発売日",
    "ランキング",
    "順位",
    "在庫",
    "セール",
    "試合",
    "選挙",
    "動向",
    # English
    "price",
    "prices",
    "pricing",
    "cost",
    "latest",
    "newest",
    "recent",
    "recently",
    "news",
    "today",
    "tonight",
    "yesterday",
    "tomorrow",
    "current",
    "currently",
    "now",
    "weather",
    "stock",
    "exchange rate",
    "score",
    "scores",
    "ranking",
    "rankings",
    "release date",
    "this week",
    "this month",
    "this year",
    "breaking",
    "forecast",
    "trending",
)


@dataclass(frozen=True)
class ReuseDecision:
    """``reuse`` is true only for ``REUSED_FRESH``. ``prior`` is set for every code except
    ``NO_PRIOR_RESEARCH``; for stale and time-sensitive it is the "previous result" context."""

    reason: ReuseReason
    topic: TopicClass
    prior: ResearchSession | None = None
    prior_at: datetime | None = None
    similarity: float | None = None

    @property
    def reuse(self) -> bool:
        return self.reason is ReuseReason.REUSED_FRESH


def normalize_question(text: str) -> str:
    """NFKC, lower-case, letters and digits only (spaces and punctuation dropped)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def _grams(compact: str) -> set[str]:
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def question_similarity(a: str, b: str) -> float:
    """0..1. 1.0 for questions equal after normalisation, else character-bigram Jaccard."""
    left, right = normalize_question(a), normalize_question(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    grams_a, grams_b = _grams(left), _grams(right)
    return len(grams_a & grams_b) / len(grams_a | grams_b)


def classify_topic(question: str) -> TopicClass:
    """Time-sensitive when the question contains a word of ``TOPIC_VOCABULARY``."""
    folded = unicodedata.normalize("NFKC", question).casefold()
    spaced = f" {' '.join(''.join(c if c.isalnum() else ' ' for c in folded).split())} "
    for word in TOPIC_VOCABULARY:
        if word.isascii():
            if f" {word} " in spaced:
                return TopicClass.TIME_SENSITIVE
        elif word in folded:
            return TopicClass.TIME_SENSITIVE
    return TopicClass.STABLE


def prior_retrieved_at(sources: list[ResearchSource]) -> datetime | None:
    """The oldest ``retrieved_at`` among the sources, or ``None`` without sources."""
    return min((s.retrieved_at for s in sources), default=None)


def decide_reuse(
    repository: ResearchRepository,
    question: str,
    level: ResearchLevel,
    now: datetime,
    *,
    exclude: UUID | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> ReuseDecision:
    """Find the best prior research for ``question`` and decide reuse or a fresh search."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    topic = classify_topic(question)
    best: tuple[float, datetime, ResearchSession] | None = None
    for session in repository.list_sessions(
        ResearchStatus.COMPLETED, limit=CANDIDATE_SCAN_LIMIT, newest_first=True
    ):
        if session.id == exclude or _LEVEL_RANK[session.level] < _LEVEL_RANK[level]:
            continue
        similarity = question_similarity(question, session.question)
        if similarity < min_similarity:
            continue
        sources = repository.list_sources(session.id)
        if not repository.list_claims(session.id):
            continue  # nothing verified: never reused
        at = prior_retrieved_at(sources)
        if at is None:
            continue
        if best is None or (similarity, at) > (best[0], best[1]):
            best = (similarity, at, session)
    if best is None:
        return ReuseDecision(ReuseReason.NO_PRIOR_RESEARCH, topic)
    similarity, at, prior = best
    if TopicClass.TIME_SENSITIVE in (topic, classify_topic(prior.question)):
        reason = ReuseReason.TIME_SENSITIVE_TOPIC
    else:
        age = now - at
        reason = (
            ReuseReason.REUSED_FRESH
            if timedelta(0) <= age <= timedelta(days=max_age_days)
            else ReuseReason.PRIOR_STALE
        )
    return ReuseDecision(reason, topic, prior, at, similarity)


def copy_prior_into(repository: ResearchRepository, prior_id: UUID, session_id: UUID) -> int:
    """Copy the prior session's sources and verified claims into ``session_id``.

    Sources keep their original ``retrieved_at`` and ratings; claims keep their quotes and
    offsets. Returns the number of claims copied.
    """
    mapping: dict[UUID, UUID] = {}
    for source in repository.list_sources(prior_id):
        copy = repository.add_source(
            session_id,
            url=source.url,
            final_url=source.final_url,
            retrieved_at=source.retrieved_at,
            content_digest=source.content_digest,
            title=source.title,
            publisher=source.publisher,
            published_at=source.published_at,
            source_type=source.source_type,
            evaluation=source.evaluation,
        )
        mapping[source.id] = copy.id
    copied = 0
    for claim in repository.list_claims(prior_id):
        if claim.source_id not in mapping:
            continue
        repository.add_claim(
            session_id,
            claim_text=claim.claim_text,
            source_id=mapping[claim.source_id],
            quote=claim.quote,
            quote_start=claim.quote_start,
            quote_end=claim.quote_end,
        )
        copied += 1
    return copied
