"""Research level selection (JAR-33): which depth of research a question gets.

The selector is a transparent, deterministic rule table. It looks only at the question
text, never calls a model or the network, and never executes anything the question says.

Precedence, always:

1. A level named by a human (``requested``) wins. It is applied as given, even above the
   configured maximum; the decision then carries ``exceeds_max=True`` so the caller can
   ask for confirmation instead of the selector silently refusing or silently obeying.
2. Otherwise the first matching rule in ``LEVEL_RULES`` decides. The result is never above
   ``max_level``: a higher automatic choice is lowered to the maximum and flagged
   ``capped=True``.
3. With no cue at all the level is ``quick`` (the conservative default).

Reasons are fixed codes (``LevelReason``), never free text. Cues are matched on the
NFKC-normalised, case-folded question. English cues match whole words; Japanese cues match
as substrings. The tables below are the documentation of the rules and are meant to be
extended in code review, one cue at a time.

``LevelBudget`` is the per-level limit table (queries, pages, time). ``quick_limits`` derives
``QuickLimits`` from it; with the ``quick`` row the result equals ``QuickLimits()``.
"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from backend.research.models import MAX_QUESTION_CHARS, ResearchLevel
from backend.research.planner import clean_text
from backend.research.quick import QuickLimits

LEVEL_ORDER: tuple[ResearchLevel, ...] = (
    ResearchLevel.MEMORY,
    ResearchLevel.QUICK,
    ResearchLevel.STANDARD,
    ResearchLevel.DEEP,
    ResearchLevel.EXTENSIVE,
)
_RANK = {level: index for index, level in enumerate(LEVEL_ORDER)}

# Extensive research is the most expensive level, so it is never chosen automatically unless
# the caller raises the maximum. A human can still ask for it explicitly.
DEFAULT_MAX_AUTO_LEVEL = ResearchLevel.DEEP

# Only this much of the question is scanned, so adversarial input cannot slow the selector.
_SCAN_CHARS = MAX_QUESTION_CHARS


class LevelReason(StrEnum):
    """Fixed codes explaining a level decision."""

    HUMAN_SPECIFIED = "human_specified"
    NO_SEARCH_REQUEST = "no_search_request"
    REPORT_CUE = "report_cue"
    MULTI_FACET_CUE = "multi_facet_cue"
    MULTI_QUESTION = "multi_question"
    COMPARISON_CUE = "comparison_cue"
    SIMPLE_FACT_CUE = "simple_fact_cue"
    MEMORY_CUE = "memory_cue"
    DEFAULT_QUICK = "default_quick"


@dataclass(frozen=True)
class LevelRule:
    """One row of the rule table: any of its cues selects ``level`` with ``reason``."""

    reason: LevelReason
    level: ResearchLevel
    english: tuple[str, ...] = ()  # regular-expression fragments, matched as whole words
    japanese: tuple[str, ...] = ()  # literal substrings


# Order matters: the first matching rule wins. Cues are written for the case-folded,
# NFKC-normalised question (so "Web" is "web" and full-width letters are ASCII).
LEVEL_RULES: tuple[LevelRule, ...] = (
    # The user says not to search at all.
    LevelRule(
        LevelReason.NO_SEARCH_REQUEST,
        ResearchLevel.MEMORY,
        english=(
            r"without (?:searching|(?:a )?web search|browsing)",
            r"(?:do not|don't|dont) (?:search|browse)",
            r"no (?:web )?search",
            r"memory only",
            r"from (?:my )?memory only",
        ),
        japanese=(
            "検索しないで",
            "検索せず",
            "検索なし",
            "ネット検索なし",
            "調べなくて",
            "記憶だけで",
            "記憶のみ",
            "メモリだけ",
            "メモリのみ",
        ),
    ),
    # A report or an explicitly exhaustive investigation.
    LevelRule(
        LevelReason.REPORT_CUE,
        ResearchLevel.EXTENSIVE,
        english=(
            r"research report",
            r"report on",
            r"(?:write|draft|prepare|produce|compile) (?:a |an |the )?(?:\w+ ){0,2}report",
            r"white ?paper",
            r"literature review",
            r"comprehensive",
            r"exhaustive(?:ly)?",
            r"thorough(?:ly)?",
            r"state of the art",
            r"extensive research",
            r"detailed (?:survey|investigation|analysis|research)",
        ),
        japanese=(
            "レポート",
            "報告書",
            "調査報告",
            "白書",
            "網羅",
            "徹底",
            "包括的",
            "文献調査",
            "サーベイ",
            "詳細な調査",
            "本格的に調査",
        ),
    ),
    # Several angles at once.
    LevelRule(
        LevelReason.MULTI_FACET_CUE,
        ResearchLevel.DEEP,
        english=(
            r"pros and cons",
            r"trade-?offs?",
            r"multi-?faceted",
            r"(?:multiple|different|various) (?:angles|perspectives|viewpoints)",
            r"in-?depth",
            r"deep dive",
        ),
        japanese=(
            "多角的",
            "多面的",
            "総合的に",
            "長所と短所",
            "メリットとデメリット",
            "メリット・デメリット",
            "トレードオフ",
            "深掘り",
            "掘り下げ",
            "詳しく調査",
        ),
    ),
    # Comparison, selection, recommendation.
    LevelRule(
        LevelReason.COMPARISON_CUE,
        ResearchLevel.STANDARD,
        english=(
            r"vs\.?",
            r"versus",
            r"compare[sd]?",
            r"comparison",
            r"compared to",
            r"differences? between",
            r"better than",
            r"alternatives? to",
            r"recommend(?:ation|ations|ed)?",
            r"which (?:is|one is|should)",
            r"should (?:i|we) (?:use|choose|pick|buy|switch|migrate)",
            r"(?:choose|pick) between",
            r"best \w+(?: \w+){0,3} for",
        ),
        japanese=(
            "比較",
            "比べ",
            "違い",
            "どちらが",
            "どっち",
            "おすすめ",
            "オススメ",
            "選定",
            "選び方",
            "乗り換え",
            "代替",
            "購入検討",
            "買うべき",
        ),
    ),
    # A single fact that may have changed recently.
    LevelRule(
        LevelReason.SIMPLE_FACT_CUE,
        ResearchLevel.QUICK,
        english=(
            r"latest",
            r"newest",
            r"current (?:version|price|status)",
            r"release date",
            r"price of",
            r"version of",
            r"when (?:was|is|did)",
            r"who (?:is|was)",
        ),
        japanese=(
            "最新",
            "現在の",
            "バージョン",
            "いつ",
            "価格",
            "値段",
            "発売日",
            "リリース日",
            "誰が",
        ),
    ),
    # A question about what the user said before. Used only when nothing above matched, so a
    # question that also needs fresh facts still gets a search.
    LevelRule(
        LevelReason.MEMORY_CUE,
        ResearchLevel.MEMORY,
        english=(
            r"do you remember",
            r"what did i (?:tell|say|ask)",
            r"did i (?:tell|mention|say)",
            r"as i (?:said|mentioned)",
            r"we (?:talked|discussed|spoke) (?:about|before|earlier)",
            r"last time we",
            r"my (?:notes|memories|preferences)",
        ),
        japanese=(
            "覚えてる",
            "覚えている",
            "前に話",
            "前に言",
            "以前話",
            "以前言",
            "さっき話",
            "この前話",
            "私の好み",
            "私の設定",
            "私のメモ",
        ),
    ),
)

_MULTI_QUESTION_RUNS = 3  # this many separate question marks suggest a multi-part question
_QUESTION_RUN = re.compile(r"[?？]+")


def _compile(rule: LevelRule) -> tuple[re.Pattern[str] | None, tuple[str, ...]]:
    pattern = None
    if rule.english:
        pattern = re.compile(r"(?<![a-z0-9])(?:" + "|".join(rule.english) + r")(?![a-z0-9])")
    return pattern, tuple(unicodedata.normalize("NFKC", cue).casefold() for cue in rule.japanese)


_COMPILED = tuple((rule, *_compile(rule)) for rule in LEVEL_RULES)


@dataclass(frozen=True)
class LevelDecision:
    """The chosen level and why.

    ``overridden`` is true when a human named the level. ``auto_level`` is what the rules
    chose before the human choice (and after the maximum cap), so a UI can show the
    difference. ``capped`` means an automatic level was lowered to ``max_level``;
    ``exceeds_max`` means a human-named level is above ``max_level`` (it is still applied).
    """

    level: ResearchLevel
    reason_code: LevelReason
    overridden: bool = False
    auto_level: ResearchLevel = ResearchLevel.QUICK
    capped: bool = False
    exceeds_max: bool = False


def _normalise(question: str) -> str:
    return clean_text(question, _SCAN_CHARS).casefold()


def _match_rules(text: str) -> tuple[ResearchLevel, LevelReason]:
    for rule, pattern, japanese in _COMPILED:
        if (pattern is not None and pattern.search(text)) or any(cue in text for cue in japanese):
            return rule.level, rule.reason
        # A multi-part question counts as a multi-facet cue, right after the explicit ones.
        if rule.reason is LevelReason.MULTI_FACET_CUE and (
            len(_QUESTION_RUN.findall(text)) >= _MULTI_QUESTION_RUNS
        ):
            return ResearchLevel.DEEP, LevelReason.MULTI_QUESTION
    return ResearchLevel.QUICK, LevelReason.DEFAULT_QUICK


def select_level(
    question: str,
    *,
    requested: ResearchLevel | None = None,
    max_level: ResearchLevel = DEFAULT_MAX_AUTO_LEVEL,
) -> LevelDecision:
    """Choose a research level. Pure and deterministic; see the module docstring.

    Raises ``ValueError`` for a non-string question or a value that is not a level. A blank
    question gets the default ``quick`` (rejecting blank questions is the session's job).
    """
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    maximum = ResearchLevel(max_level)
    human = None if requested is None else ResearchLevel(requested)
    auto, reason = _match_rules(_normalise(question))
    capped = _RANK[auto] > _RANK[maximum]
    if capped:
        auto = maximum
    if human is not None:
        return LevelDecision(
            level=human,
            reason_code=LevelReason.HUMAN_SPECIFIED,
            overridden=True,
            auto_level=auto,
            capped=False,
            exceeds_max=_RANK[human] > _RANK[maximum],
        )
    return LevelDecision(level=auto, reason_code=reason, auto_level=auto, capped=capped)


@dataclass(frozen=True)
class LevelBudget:
    """Upper limits for one level. These are proposals for the pipelines of later slices.

    Only the ``quick`` row is used today (through ``quick_limits``); the other rows are
    ceilings a future Standard/Deep pipeline must not exceed, so cost cannot grow silently.
    """

    level: ResearchLevel
    max_queries: int
    results_per_query: int
    max_pages: int
    max_search_rounds: int
    total_timeout_seconds: float

    def __post_init__(self) -> None:
        ResearchLevel(self.level)
        counts = (self.max_queries, self.results_per_query, self.max_pages, self.max_search_rounds)
        for value in counts:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("budget counts must be non-negative integers")
        if self.results_per_query > 20:
            raise ValueError("results_per_query must be at most 20")
        timeout = self.total_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout < 0:
            raise ValueError("total_timeout_seconds must be a non-negative number")
        zero = [value == 0 for value in (*counts, timeout)]
        if self.level is ResearchLevel.MEMORY:
            if not all(zero):
                raise ValueError("the memory level allows no searching")
        elif any(zero):
            raise ValueError("every limit of a searching level must be positive")


LEVEL_BUDGETS: Mapping[ResearchLevel, LevelBudget] = MappingProxyType(
    {
        ResearchLevel.MEMORY: LevelBudget(ResearchLevel.MEMORY, 0, 0, 0, 0, 0.0),
        ResearchLevel.QUICK: LevelBudget(ResearchLevel.QUICK, 2, 5, 3, 1, 120.0),
        ResearchLevel.STANDARD: LevelBudget(ResearchLevel.STANDARD, 5, 6, 8, 2, 300.0),
        ResearchLevel.DEEP: LevelBudget(ResearchLevel.DEEP, 10, 8, 20, 3, 900.0),
        ResearchLevel.EXTENSIVE: LevelBudget(ResearchLevel.EXTENSIVE, 20, 10, 40, 5, 1800.0),
    }
)


def budget_for(level: ResearchLevel) -> LevelBudget:
    return LEVEL_BUDGETS[ResearchLevel(level)]


def quick_limits(level: ResearchLevel = ResearchLevel.QUICK) -> QuickLimits:
    """``QuickLimits`` derived from a level's budget; the ``memory`` level has none."""
    budget = budget_for(level)
    if budget.level is ResearchLevel.MEMORY:
        raise ValueError("the memory level does not search")
    return QuickLimits(
        max_queries=budget.max_queries,
        results_per_query=budget.results_per_query,
        max_pages=budget.max_pages,
        total_timeout=budget.total_timeout_seconds,
    )
