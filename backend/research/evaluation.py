"""Source evaluation (JAR-43, JAR-44, JAR-45): independent 0..1 ratings, with reasons.

Three ratings are produced and written into ``SourceEvaluation``; each is computed on its own
and none uses another:

* ``authority``: how much institutional standing the kind of site has, from the
  ``SourceType`` (``classification.py``). It is NOT popularity, traffic, search rank or
  correctness. A high-authority source can be wrong, and a low-authority one right.
* ``freshness``: ``0.5 ** (age / half_life)``, where age is ``retrieved_at - published_at``
  and the half-life depends on how fast the topic changes (``HALF_LIFE_DAYS``). An unknown
  publication date gives ``None``; a date is never invented, and a date after the retrieval
  time (beyond one day of clock slack) is treated as unknown, not as "fresh". Freshness is
  not quality: for stable topics an old source is fine, and the half-life says so.
* ``relevance``: lexical overlap between the question's terms and the title/extracted text.
  This is a cheap baseline and NOT semantic: it cannot see synonyms, negation or whether
  the page really answers the question, and it can be inflated by keyword stuffing. English
  is tokenised into lower-cased words (stop words removed, a trailing plural ``s`` dropped);
  Japanese is split at common particles and cut into character bigrams, so it needs no
  dictionary. Single characters are matched as substrings.

``primary`` and ``agreement`` (JAR-46) are left untouched (``None`` unless already set).

All ratings are heuristic inputs for a person or a later synthesis step. They are not truth
values and not probabilities, and two sources with equal ratings are not equally correct.
Ratings, reasons and the classification basis are fixed codes and numbers; no text from the
page or the question is copied into them.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from backend.research.classification import (
    SUBJECT_OFFICIAL_AUTHORITY_CAP,
    SUBJECT_OFFICIAL_RULE,
    SourceClassification,
    classify_source_detailed,
)
from backend.research.models import (
    MAX_QUESTION_CHARS,
    MAX_TITLE_CHARS,
    Basis,
    RatingName,
    RatingReason,
    ResearchSource,
    SourceEvaluation,
    SourceType,
)
from backend.research.planner import clean_text
from backend.research.repository import ResearchRepository
from backend.research.terms import extract_terms, latin_terms, query_latin_terms, subject_terms

MAX_TEXT_CHARS = 20_000  # the reader's own text cap
MAX_QUESTION_TERMS = 64
_FUTURE_SLACK = timedelta(days=1)
_DIGITS = 4
_MAX_URL_SCAN = 2048
_URL_SEPARATORS = re.compile(r"[/?#&=:%]+")


class TopicClass(StrEnum):
    """How fast the subject of a question changes."""

    BREAKING = "breaking"  # news, prices, scores: days matter
    FAST = "fast"  # software versions, security advisories, pricing
    STANDARD = "standard"  # general technical or product questions (the default)
    STABLE = "stable"  # history, mathematics, fundamentals


HALF_LIFE_DAYS: Mapping[TopicClass, int] = MappingProxyType(
    {
        TopicClass.BREAKING: 30,
        TopicClass.FAST: 180,
        TopicClass.STANDARD: 730,
        TopicClass.STABLE: 3650,
    }
)

# Heuristic prior by kind of source, following the design's source priority (official
# documentation, primary sources, papers, reputable news, community, personal blogs).
AUTHORITY_BY_TYPE: Mapping[SourceType, float] = MappingProxyType(
    {
        SourceType.OFFICIAL: 0.9,
        SourceType.DOCS: 0.85,
        SourceType.ACADEMIC: 0.75,
        SourceType.NEWS: 0.6,
        SourceType.COMMUNITY: 0.4,
        SourceType.FORUM: 0.35,
        SourceType.BLOG: 0.3,
        SourceType.UNKNOWN: 0.25,
    }
)
# A type inferred from a path or a title is easier to fake than one inferred from the host.
AUTHORITY_CAP_BY_BASIS: Mapping[Basis, float] = MappingProxyType(
    {Basis.PATH: 0.6, Basis.TITLE: 0.35}
)

# Topic cues on the case-folded NFKC question: English as whole words, Japanese as substrings.
_TOPIC_CUES: tuple[tuple[TopicClass, re.Pattern[str], tuple[str, ...]], ...] = (
    (
        TopicClass.BREAKING,
        re.compile(
            r"(?<![a-z0-9])(?:news|breaking|today|stock price|exchange rate|weather|live score)"
            r"(?![a-z0-9])"
        ),
        ("ニュース", "速報", "今日", "株価", "為替", "天気"),
    ),
    (
        TopicClass.FAST,
        re.compile(
            r"(?<![a-z0-9])(?:latest|newest|version|release|update|cve-[0-9-]+"
            r"|vulnerabilit(?:y|ies)|pricing|price)(?![a-z0-9])"
        ),
        ("最新", "バージョン", "リリース", "アップデート", "脆弱性", "料金", "価格", "現在"),
    ),
    (
        TopicClass.STABLE,
        re.compile(
            r"(?<![a-z0-9])(?:history of|theorem|proof|definition of|principles? of|algorithm"
            r"|mathematics)(?![a-z0-9])"
        ),
        ("歴史", "定理", "証明", "原理", "アルゴリズム", "数学"),
    ),
)


def _clean(text: str, limit: int) -> str:
    return clean_text(text, limit).casefold()


def infer_topic_class(question: str) -> TopicClass:
    """Topic class from cue words in the question; ``standard`` when none match."""
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    text = _clean(question, MAX_QUESTION_CHARS)
    for topic, pattern, japanese in _TOPIC_CUES:
        if pattern.search(text) or any(cue in text for cue in japanese):
            return topic
    return TopicClass.STANDARD


# ----- ratings -----


def _check_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def authority_rating(
    source_type: SourceType, basis: Basis | None = None, rule_id: str | None = None
) -> tuple[float, RatingReason]:
    """Authority prior for a kind of source; ``basis`` (how the type was found) may cap it.

    A source found by the ``subject_official_host`` rule (the site is named after the question's
    subject) is capped at ``SUBJECT_OFFICIAL_AUTHORITY_CAP``: the name pattern is not proof.
    """
    source_type = SourceType(source_type)
    base = AUTHORITY_BY_TYPE[source_type]
    if rule_id == SUBJECT_OFFICIAL_RULE and source_type is not SourceType.UNKNOWN:
        return min(base, SUBJECT_OFFICIAL_AUTHORITY_CAP), RatingReason.AUTHORITY_SUBJECT_OFFICIAL
    if source_type is SourceType.UNKNOWN:
        return base, RatingReason.AUTHORITY_UNCLASSIFIED
    cap = AUTHORITY_CAP_BY_BASIS.get(basis) if basis is not None else None
    if cap is not None and base > cap:
        return cap, RatingReason.AUTHORITY_CAPPED_WEAK_BASIS
    return base, RatingReason.AUTHORITY_BY_TYPE


def freshness_rating(
    published_at: datetime | None, retrieved_at: datetime, topic_class: TopicClass
) -> tuple[float | None, RatingReason]:
    """Exponential decay with the topic's half-life; ``None`` when the date is unusable."""
    _check_aware("retrieved_at", retrieved_at)
    half_life = HALF_LIFE_DAYS[TopicClass(topic_class)]
    if published_at is None:
        return None, RatingReason.FRESHNESS_UNKNOWN_DATE
    _check_aware("published_at", published_at)
    age = retrieved_at - published_at
    if age < -_FUTURE_SLACK:
        return None, RatingReason.FRESHNESS_FUTURE_DATE
    days = max(age.total_seconds(), 0.0) / 86400
    return round(2.0 ** (-days / half_life), _DIGITS), RatingReason.FRESHNESS_DECAY


LATIN_QUERY_SHARE = 0.2  # of the Latin score: terms only the issued queries added (guide, ...)
LATIN_SHARE = 0.85  # with Latin terms present, they carry this share of the score
MAX_QUERY_ONLY_TERMS = 16


def _overlap(
    wanted: list[tuple[str, float]],
    title_terms: set[str],
    anywhere_terms: set[str],
    singles: set[str],
    title_raw: str,
    both_raw: str,
) -> float:
    """``0.75 * weighted share found anywhere + 0.25 * weighted share found in title or URL``."""
    total = sum(weight for _, weight in wanted)
    if total <= 0:
        return 0.0
    anywhere = in_title = 0.0
    for term, weight in wanted:
        single = term in singles
        if term in anywhere_terms or (single and term in both_raw):
            anywhere += weight
        if term in title_terms or (single and term in title_raw):
            in_title += weight
    return (0.75 * anywhere + 0.25 * in_title) / total


def relevance_rating(
    question: str,
    title: str | None,
    text: str | None,
    *,
    url: str | None = None,
    queries: Sequence[str] = (),
) -> tuple[float | None, RatingReason]:
    """Lexical overlap of the question's terms with the title, URL and text (not semantic).

    Terms are the Latin/ASCII technical tokens (``SQLite``, ``WAL``) and the CJK terms of the
    question, plus the Latin tokens of the issued ``queries`` (a Japanese question is often
    searched with English terms). Each group is scored as
    ``0.75 * share found anywhere + 0.25 * share found in the title or URL`` (host, path and
    title count as the title). The Latin tokens the asker wrote make 80% of the Latin score and
    the tokens only the queries added (often generic: "advantages", "guide") 20%, so the rare
    technical terms outweigh generic words. When the question has
    Latin terms they carry 85% of the score: a page that shares only Japanese filler bigrams
    with the question, and none of its technical terms, scores at most 0.15, while an English
    page that names them scores high. Query-only terms count only when the page also has one of
    the asker's own terms.
    Without Latin terms the CJK overlap is the score.
    """
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    folded_question = _clean(question, MAX_QUESTION_CHARS)
    q_terms, q_singles = extract_terms(folded_question)
    q_latin = {term for term in q_terms if term.isascii()}
    cjk_wanted = (sorted(q_terms - q_latin) + sorted(q_singles))[:MAX_QUESTION_TERMS]
    q_latin_wanted = sorted(q_latin)[:MAX_QUESTION_TERMS]
    query_only = sorted(query_latin_terms(queries)[0] - q_latin)[:MAX_QUERY_ONLY_TERMS]
    if not cjk_wanted and not q_latin_wanted:
        return None, RatingReason.RELEVANCE_NO_TERMS
    title_text = _clean(title, MAX_TITLE_CHARS) if isinstance(title, str) else ""
    body_text = _clean(text, MAX_TEXT_CHARS) if isinstance(text, str) else ""
    url_text = _clean(url, _MAX_URL_SCAN).casefold() if isinstance(url, str) else ""
    if not title_text.strip() and not body_text.strip():
        return None, RatingReason.RELEVANCE_NO_TEXT
    title_cjk, _ = extract_terms(title_text)
    body_cjk, _ = extract_terms(body_text)
    head_latin = latin_terms(title_text, split_compounds=True) | latin_terms(
        _URL_SEPARATORS.sub(" ", url_text), split_compounds=True
    )
    anywhere_latin = head_latin | latin_terms(body_text, split_compounds=True)
    both_raw = title_text + " " + body_text
    singles = set(q_singles)

    cjk_score = _overlap(
        [(term, 1.0) for term in cjk_wanted],
        title_cjk,
        title_cjk | body_cjk,
        singles,
        title_text,
        both_raw,
    )
    reason = (
        RatingReason.RELEVANCE_OVERLAP if body_text.strip() else RatingReason.RELEVANCE_TITLE_ONLY
    )
    if not q_latin_wanted:
        # No technical term in the question: the CJK overlap decides.
        return round(min(cjk_score, 1.0), _DIGITS), reason
    asked = _overlap(
        [(term, 1.0) for term in q_latin_wanted], head_latin, anywhere_latin, set(), "", ""
    )
    latin_score = asked
    if query_only and asked > 0:
        # Generic words the queries added count only when the page also has one of the asker's
        # own terms (a page that merely says "guide" or "overview" gains nothing).
        added = _overlap(
            [(term, 1.0) for term in query_only], head_latin, anywhere_latin, set(), "", ""
        )
        latin_score = (1 - LATIN_QUERY_SHARE) * asked + LATIN_QUERY_SHARE * added
    if not cjk_wanted:
        return round(min(latin_score, 1.0), _DIGITS), reason
    score = LATIN_SHARE * latin_score + (1 - LATIN_SHARE) * cjk_score
    return round(min(score, 1.0), _DIGITS), reason


# ----- assembling -----


@dataclass(frozen=True)
class SourceAssessment:
    """Ratings plus the codes that explain them. ``classification_rule`` is a rule id."""

    evaluation: SourceEvaluation
    source_type: SourceType
    classification_rule: str
    classification_basis: Basis
    topic_class: TopicClass
    authority_reason: RatingReason
    freshness_reason: RatingReason
    relevance_reason: RatingReason


def assess_source(
    *,
    question: str,
    url: str,
    retrieved_at: datetime,
    title: str | None = None,
    text: str | None = None,
    published_at: datetime | None = None,
    source_type: SourceType | None = None,
    topic_class: TopicClass | None = None,
    classification_rule: str | None = None,
    classification_basis: Basis | None = None,
    queries: Sequence[str] = (),
) -> SourceAssessment:
    """Rate one source. A non-``unknown`` ``source_type`` is trusted; otherwise it is classified.

    When a trusted ``source_type`` comes with the ``classification_rule`` and
    ``classification_basis`` that decided it (as stored by ``set_source_classification``),
    they are kept and the authority cap for weak bases still applies; without them the type
    counts as ``provided`` and is not capped.

    ``text`` is an excerpt of the extracted page (page text is not stored, so the caller
    that read the page supplies it); only its first 20,000 characters are used. ``queries``
    are the search queries issued for the question: their Latin terms count for relevance and,
    together with the question's, for the ``subject_official_host`` classification.
    """
    if source_type is not None and SourceType(source_type) is not SourceType.UNKNOWN:
        if classification_rule is not None and classification_basis is not None:
            classification = SourceClassification(
                SourceType(source_type), classification_rule, Basis(classification_basis)
            )
        else:
            classification = SourceClassification(
                SourceType(source_type), "provided", Basis.PROVIDED
            )
    else:
        classification = classify_source_detailed(
            url, title, subject_terms=subject_terms(question, queries)
        )
    topic = TopicClass(topic_class) if topic_class is not None else infer_topic_class(question)
    basis = None if classification.basis is Basis.PROVIDED else classification.basis
    authority, authority_reason = authority_rating(
        classification.source_type, basis, classification.rule_id
    )
    freshness, freshness_reason = freshness_rating(published_at, retrieved_at, topic)
    relevance, relevance_reason = relevance_rating(
        question, title, text, url=url, queries=queries
    )
    return SourceAssessment(
        evaluation=SourceEvaluation(authority=authority, freshness=freshness, relevance=relevance),
        source_type=classification.source_type,
        classification_rule=classification.rule_id,
        classification_basis=classification.basis,
        topic_class=topic,
        authority_reason=authority_reason,
        freshness_reason=freshness_reason,
        relevance_reason=relevance_reason,
    )


def evaluate_source(
    *,
    question: str,
    url: str,
    retrieved_at: datetime,
    title: str | None = None,
    text: str | None = None,
    published_at: datetime | None = None,
    source_type: SourceType | None = None,
    topic_class: TopicClass | None = None,
) -> SourceEvaluation:
    """The ratings of ``assess_source`` without the reasons."""
    return assess_source(
        question=question,
        url=url,
        retrieved_at=retrieved_at,
        title=title,
        text=text,
        published_at=published_at,
        source_type=source_type,
        topic_class=topic_class,
    ).evaluation


def evaluate_and_store(
    repository: ResearchRepository,
    source: ResearchSource,
    question: str,
    *,
    text: str | None = None,
    topic_class: TopicClass | None = None,
    queries: Sequence[str] = (),
) -> ResearchSource:
    """Rate a stored source and save its type, authority, freshness and relevance.

    The classified source type, the rule and basis that decided it, and the reason code of
    each of the three ratings are stored with the ratings. Existing ``primary`` and
    ``agreement`` ratings (and the agreement reason codes) are kept. A type that was already
    stored with its rule and basis is not reclassified. Raises what ``set_evaluation``
    raises, for example when the session is already final.
    """
    assessment = assess_source(
        question=question,
        url=source.final_url,
        retrieved_at=source.retrieved_at,
        title=source.title,
        text=text,
        published_at=source.published_at,
        source_type=source.source_type,
        topic_class=topic_class,
        classification_rule=source.classification_rule,
        classification_basis=source.classification_basis,
        queries=queries,
    )
    merged = replace(
        source.evaluation,
        authority=assessment.evaluation.authority,
        freshness=assessment.evaluation.freshness,
        relevance=assessment.evaluation.relevance,
    )
    return repository.set_source_assessment(
        source.id,
        source_type=assessment.source_type,
        rule_id=assessment.classification_rule,
        basis=assessment.classification_basis,
        evaluation=merged,
        reasons={
            RatingName.AUTHORITY: (assessment.authority_reason,),
            RatingName.FRESHNESS: (assessment.freshness_reason,),
            RatingName.RELEVANCE: (assessment.relevance_reason,),
        },
    )
