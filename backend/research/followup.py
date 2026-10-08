"""Follow-up queries (JAR-48): bounded extra search queries for the gaps still unmet.

``search_decision.decide_additional_search`` says WHETHER to search again and returns a gap
reason code (``no_sources``, ``few_relevant_sources``, ``unresolved_conflicts``,
``no_authoritative_source``, ``stale_sources``). This module turns those codes into the
queries to run. It is pure: no model, no network, no clock, no state.

How a query is built. Every query is ``<subject> <fixed phrase>`` where

* ``subject`` is the question's own content terms (planner v2 keywords, at most
  ``MAX_SUBJECT_TERMS``), so a follow-up stays about what the user asked;
* the fixed phrase comes from a template table per gap (English or Japanese, chosen by the
  question's language). The tables below are the documentation of the rules;
* some templates add up to ``MAX_TITLE_TERMS`` short terms taken from the titles of sources
  that were already found (``{extra}``).

Page-derived text is untrusted. A source title is the only such input, and only a handful of
short, plain terms survive: letters and digits (or a katakana/kanji run) of at most
``MAX_TERM_CHARS`` characters, never a URL, a path, an operator or a sentence, never a term
the question already has, never a generic word or an instruction-like word
(``BLOCKED_TERMS``). Page bodies and snippets are never read here. A query is bounded by
``MAX_QUERY_CHARS``, is cleaned like every planner query, and differs from every query that
already ran (case, spacing and word order do not count as different). Each query carries the
one gap it answers and the id of the template that produced it, so a UI can show why it ran.

The result is deterministic: the same inputs give the same queries in the same order.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from backend.research.models import MAX_QUESTION_CHARS
from backend.research.planner import EN_STOPWORDS, DeterministicQueryPlanner, clean_text
from backend.research.search_decision import DecisionReason, Gap

MAX_FOLLOW_UP_QUERIES = 3  # hard ceiling per call, whatever the caller asks for
MAX_QUERY_CHARS = 160
MAX_SUBJECT_TERMS = 6
MAX_TITLE_TERMS = 2
MAX_TERM_CHARS = 24
MAX_TITLES_READ = 12
MAX_TITLE_CHARS = 200
MAX_EXISTING_QUERIES = 200

_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[.\-][A-Za-z0-9+#]+)*")
_CJK_TERM = re.compile(r"[ァ-ヶー一-龥々]{2,12}")

# Words that describe the page rather than the subject, plus words that look like an attempt
# to instruct a reader. A title term in either list is dropped. This is defence in depth: the
# query only goes to a search engine, never to a model, and is bounded anyway.
GENERIC_TERMS: Final = frozenset(
    "docs documentation official home homepage page index welcome untitled blog news wiki "
    "guide tutorial overview introduction article post posts forum thread question answer "
    "answers reference manual faq help support download downloads login signin signup "
    "github stackoverflow reddit medium youtube www http https com org net html".split()
)
BLOCKED_TERMS: Final = frozenset(
    "ignore ignored previous instruction instructions system prompt assistant developer "
    "admin administrator override reveal disregard jailbreak password token credential "
    "credentials secrets email send fetch visit click delete execute".split()
)


@dataclass(frozen=True)
class _Template:
    template_id: str
    pattern: str  # uses {kw} and optionally {extra}


# Gap -> templates in priority order. The first unused, non-duplicate one wins.
_EN: Final[dict[Gap, tuple[_Template, ...]]] = {
    Gap.NO_SOURCES: (
        _Template("broaden_keywords", "{kw}"),
        _Template("broaden_overview", "{kw} overview"),
        _Template("broaden_guide", "{kw} guide"),
    ),
    Gap.FEW_RELEVANT_SOURCES: (
        _Template("more_explained", "{kw} explained"),
        _Template("more_related_terms", "{kw} {extra}"),
        _Template("more_tutorial", "{kw} tutorial"),
    ),
    Gap.UNRESOLVED_CONFLICTS: (
        _Template("conflict_official", "{kw} official documentation"),
        _Template("conflict_specification", "{kw} specification"),
        _Template("conflict_clarification", "{kw} clarification"),
    ),
    Gap.NO_AUTHORITATIVE_SOURCE: (
        _Template("authority_official", "{kw} official documentation"),
        _Template("authority_site", "{kw} official site"),
        _Template("authority_related_terms", "{kw} {extra} official"),
        _Template("authority_reference", "{kw} reference specification"),
    ),
    Gap.STALE_SOURCES: (
        _Template("fresh_latest", "{kw} latest"),
        _Template("fresh_release_notes", "{kw} release notes"),
        _Template("fresh_news", "{kw} news update"),
    ),
}
_JA: Final[dict[Gap, tuple[_Template, ...]]] = {
    Gap.NO_SOURCES: (
        _Template("broaden_keywords", "{kw}"),
        _Template("broaden_overview", "{kw} 概要"),
        _Template("broaden_guide", "{kw} ガイド"),
    ),
    Gap.FEW_RELEVANT_SOURCES: (
        _Template("more_explained", "{kw} 解説"),
        _Template("more_related_terms", "{kw} {extra}"),
        _Template("more_tutorial", "{kw} 使い方"),
    ),
    Gap.UNRESOLVED_CONFLICTS: (
        _Template("conflict_official", "{kw} 公式ドキュメント"),
        _Template("conflict_specification", "{kw} 仕様"),
        _Template("conflict_clarification", "{kw} 訂正"),
    ),
    Gap.NO_AUTHORITATIVE_SOURCE: (
        _Template("authority_official", "{kw} 公式ドキュメント"),
        _Template("authority_site", "{kw} 公式サイト"),
        _Template("authority_related_terms", "{kw} {extra} 公式"),
        _Template("authority_reference", "{kw} リファレンス"),
    ),
    Gap.STALE_SOURCES: (
        _Template("fresh_latest", "{kw} 最新"),
        _Template("fresh_release_notes", "{kw} リリースノート"),
        _Template("fresh_news", "{kw} ニュース"),
    ),
}

_FROM_DECISION: Final = {
    DecisionReason.NO_SOURCES: Gap.NO_SOURCES,
    DecisionReason.FEW_RELEVANT_SOURCES: Gap.FEW_RELEVANT_SOURCES,
    DecisionReason.UNRESOLVED_CONFLICTS: Gap.UNRESOLVED_CONFLICTS,
    DecisionReason.NO_AUTHORITATIVE_SOURCE: Gap.NO_AUTHORITATIVE_SOURCE,
    DecisionReason.STALE_SOURCES: Gap.STALE_SOURCES,
}


@dataclass(frozen=True)
class FollowUpQuery:
    """One generated query with the gap it answers and the template that produced it."""

    text: str
    reason: Gap
    template_id: str


def gap_for_reason(reason: Gap | DecisionReason) -> Gap:
    """The gap a decision reason stands for. Raises ``ValueError`` for any other reason."""
    if isinstance(reason, Gap):
        return reason
    if isinstance(reason, DecisionReason) and reason in _FROM_DECISION:
        return _FROM_DECISION[reason]
    raise ValueError("reason is not a search gap")


def query_key(text: str) -> tuple[str, ...]:
    """Identity of a query for de-duplication: its sorted, case-folded words."""
    return tuple(sorted(set(clean_text(text).casefold().split())))


def title_terms(titles: Iterable[str], exclude: Iterable[str] = ()) -> tuple[str, ...]:
    """The few plain terms worth adding from source titles; see the module docstring.

    Terms are ranked by how many titles carry them (more is better), then by first
    appearance, so the order is deterministic. ``exclude`` are words the query already has.
    """
    skip = {word.casefold() for word in exclude}
    surface: dict[str, str] = {}  # folded term -> first spelling, in order of appearance
    counts: dict[str, int] = {}
    for title in list(titles)[:MAX_TITLES_READ]:
        if not isinstance(title, str):
            continue
        text = clean_text(title, MAX_TITLE_CHARS)
        matches = sorted(
            [*_LATIN_TERM.finditer(text), *_CJK_TERM.finditer(text)], key=lambda m: m.start()
        )
        in_this_title: set[str] = set()
        for match in matches:
            term = match.group().strip(".-")
            folded = term.casefold()
            if (
                not 3 <= len(term) <= MAX_TERM_CHARS
                or term.isdigit()
                or folded in skip
                or folded in EN_STOPWORDS
                or folded in GENERIC_TERMS
                or folded in BLOCKED_TERMS
                or folded in in_this_title
            ):
                continue
            in_this_title.add(folded)
            surface.setdefault(folded, term)
            counts[folded] = counts.get(folded, 0) + 1
    position = {folded: index for index, folded in enumerate(surface)}
    ranked = sorted(counts, key=lambda folded: (-counts[folded], position[folded]))
    return tuple(surface[folded] for folded in ranked[:MAX_TITLE_TERMS])


def _join_within(terms: Sequence[str], limit: int) -> str:
    out = ""
    for term in terms:
        candidate = f"{out} {term}".strip()
        if len(candidate) > limit:
            break
        out = candidate
    return out


def _fill(pattern: str, subject: Sequence[str], extra: Sequence[str]) -> str | None:
    """Fill a template, keeping the fixed phrase whole and shortening the subject to fit."""
    if "{extra}" in pattern and not extra:
        return None
    fixed = pattern.replace("{kw}", "").replace("{extra}", " ".join(extra))
    room = MAX_QUERY_CHARS - len(" ".join(fixed.split())) - 1
    keywords = _join_within(subject, room)
    if not keywords:
        return None
    text = " ".join(pattern.replace("{kw}", keywords).replace("{extra}", " ".join(extra)).split())
    return text if 0 < len(text) <= MAX_QUERY_CHARS else None


def generate_follow_ups(
    question: str,
    gaps: Sequence[Gap | DecisionReason],
    *,
    existing_queries: Iterable[str] = (),
    source_titles: Iterable[str] = (),
    max_queries: int = 2,
) -> tuple[FollowUpQuery, ...]:
    """Up to ``max_queries`` new queries for the unmet ``gaps`` (most urgent first).

    Gaps are served round-robin in the given order, each from its own template list, so the
    first gap gets the first query. A candidate that repeats an existing query (see
    ``query_key``) or an earlier candidate is skipped and the next template is tried. The
    result may be shorter than asked, or empty when the question has no usable terms, no
    gap is given, or every template repeats something that already ran. Raises
    ``ValueError`` for a non-string question, a bad ``max_queries`` or a reason that is not a
    gap; never reads the network.
    """
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or max_queries < 0:
        raise ValueError("max_queries must be a non-negative integer")
    wanted = min(max_queries, MAX_FOLLOW_UP_QUERIES)
    ordered: list[Gap] = []
    for item in gaps:
        gap = gap_for_reason(item)
        if gap not in ordered:
            ordered.append(gap)
    if wanted == 0 or not ordered:
        return ()

    plan = DeterministicQueryPlanner().plan_detailed(question[:MAX_QUESTION_CHARS], 1)
    subject = list(plan.hints.keywords[:MAX_SUBJECT_TERMS])
    if not subject:
        return ()
    templates = _JA if plan.hints.language == "ja" else _EN
    extra = title_terms(source_titles, exclude=[*subject, *question.split()])

    seen = {query_key(text) for text in list(existing_queries)[:MAX_EXISTING_QUERIES]}
    queues = {gap: list(templates[gap]) for gap in ordered}
    found: list[FollowUpQuery] = []
    while len(found) < wanted and any(queues.values()):
        for gap in ordered:
            while queues[gap]:
                template = queues[gap].pop(0)
                text = _fill(template.pattern, subject, extra)
                if text is None or query_key(text) in seen:
                    continue
                seen.add(query_key(text))
                found.append(FollowUpQuery(text, gap, template.template_id))
                break
            if len(found) >= wanted:
                break
    return tuple(found)
