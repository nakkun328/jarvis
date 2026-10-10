"""Deterministic Query Planner v2 (JAR-34): a few search queries from one question.

No model and no network. The planner only rearranges the question's own words, so it can
never add a fact the user did not ask about. Every word of every query is a word (or a
script run) of the normalised question; the only text that is not a copy is the joining
space. The planner satisfies the ``QueryPlanner`` protocol of ``quick.py`` (``plan``) and
can be passed as ``QuickResearch(..., planner=DeterministicQueryPlanner())``.

Queries, in priority order (each is optional; duplicates are dropped):

1. ``question``: the whole question, normalised and bounded.
2. ``target``: one query per comparison target (``A vs B``, ``compare A and B``,
   ``AとBの違い``, ...), each target followed by the question's remaining content words.
3. ``time``: the content words followed by the recency words of the question
   (``latest``, ``最新``, an explicit year). Only when the question has such words.
4. ``keywords``: the content words alone.

The result is cut to ``max_queries``. With a small budget the later forms drop first, so
a comparison question under a budget of two gets the question and the first target only;
the level table gives comparison questions more room.

Hints (language, recency, targets) are returned by ``plan_detailed`` for later use (for
example ``SearchQuery.language`` and ``recency_days``); Quick Research does not use them yet.

Later LLM planner hook: an LLM planner is another class with the same ``plan`` signature,
chosen through the ``planner=`` argument. Whatever it proposes must go through
``finalize_queries`` (clean, bound, de-duplicate, cut) before reaching search, and this
deterministic planner remains the fallback when the model fails. The question and all
text derived from it stay data; an LLM planner must not receive page text.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from backend.research.models import MAX_QUERY_CHARS

MAX_KEYWORD_TERMS = 10
MAX_KEYWORD_QUERY_CHARS = 200
MAX_TARGETS = 4
MAX_TARGET_CHARS = 60
RECENCY_WINDOW_DAYS = 365  # suggested SearchQuery.recency_days when the question asks for recent

_SCAN_CHARS = 2000

EN_STOPWORDS = frozenset(
    "a an and are as at be been being by can could do does for from had has have how i if in "
    "into is it its me my of on or our please should so than that the their them then there "
    "these they this to us was we were what when where which who why will with would you your "
    "about tell explain give show find".split()
)
# Words that name the kind of question rather than its subject.
_COMPARISON_WORDS = frozenset(
    "vs vs. versus compare compares compared comparison difference differences between "
    "better best which choose pick or and".split()
)
_JA_COMPARISON_CHUNKS = frozenset({"比較", "違い", "差", "どちら", "どっち"})
_JA_STOP_CHUNKS = frozenset({"説明", "解説", "質問", "教え", "調査"})

# Recency cues: English as whole words, Japanese as substrings, plus explicit years.
_RECENCY_EN = re.compile(
    r"(?<![a-z0-9])(?:latest|newest|current|currently|recent|recently|today|up[- ]to[- ]date"
    r"|this (?:year|month)|as of)(?![a-z0-9])",
    re.IGNORECASE,
)
_RECENCY_JA = ("最新", "現在", "最近", "直近", "今年", "今月", "今日", "現行")
_YEAR = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")

_ASCII_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+#\-]*")
_CJK_RUN = re.compile(r"[ァ-ヶー一-龥々]+")
_WORD_EDGE = ".,;:!?\"'()[]{}<>"
_HAS_KANA = re.compile(r"[ぁ-んァ-ヶ]")
_HAS_CJK = re.compile(r"[ぁ-んァ-ヶ一-龥々]")
_HAS_LATIN = re.compile(r"[A-Za-z]")

# One term of a Japanese comparison: a run of ASCII, katakana and kanji (hiragana ends it).
_JA_TERM = r"[A-Za-z0-9ァ-ヶー一-龥々][A-Za-z0-9.+#_\-ァ-ヶー一-龥々]*"
_JA_CUE = (
    r"(?:の?(?:比較|違い|差|どちら|どっち)|を比較|と比較|を比べ|で?は?(?:どちら|どっち)"
    r"|の?(?:方|ほう)が)"
)
_JA_PAIR = re.compile(
    rf"(?P<a>{_JA_TERM})\s*(?:と|か|vs\.?|対)\s*(?P<b>{_JA_TERM})\s*[、,]?\s*{_JA_CUE}"
)
_EN_VS = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)
_EN_BETWEEN = re.compile(
    r"(?:compare|comparison of|differences? between|choose between|pick between)\s+"
    r"(?P<a>[\w.+#\-]+(?: [\w.+#\-]+){0,2}?)\s+(?:and|with|to|or)\s+"
    r"(?P<b>[\w.+#\-]+(?: [\w.+#\-]+){0,2})",
    re.IGNORECASE,
)
_EN_OR = re.compile(
    r"(?:which is better|should (?:i|we) (?:use|choose|pick|buy))[,:]?\s+"
    r"(?P<a>[\w.+#\-]+)\s+or\s+(?P<b>[\w.+#\-]+)",
    re.IGNORECASE,
)
_PHRASE_STOP = frozenset("in for on at with when about to of is are the a an".split())


class QueryKind(StrEnum):
    QUESTION = "question"
    TARGET = "target"
    TIME = "time"
    KEYWORDS = "keywords"


@dataclass(frozen=True)
class PlannedQuery:
    text: str
    kind: QueryKind


@dataclass(frozen=True)
class QueryHints:
    """What the planner noticed in the question. All values are copied or derived by rule."""

    language: str | None  # "ja", "en" or None when unclear
    recency_terms: tuple[str, ...]  # the question's own recency words and years
    suggested_recency_days: int | None
    comparison_targets: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class QueryPlan:
    queries: tuple[PlannedQuery, ...]
    hints: QueryHints


def clean_text(value: str, limit: int = _SCAN_CHARS) -> str:
    """NFKC, control characters to spaces, invisible format characters removed, spaces collapsed.

    Only the first ``limit`` characters are read. Never raises on any ``str``.
    """
    text = unicodedata.normalize("NFKC", value[:limit])
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cc":
            out.append(" ")
        elif category not in {"Cf", "Cs", "Co"}:
            out.append(ch)
    return " ".join("".join(out).split())


def _clean_query(candidate: str) -> str:
    return clean_text(candidate)[:MAX_QUERY_CHARS].strip()


def finalize_queries(candidates: Iterable[str], max_queries: int) -> list[str]:
    """Clean, bound, de-duplicate (case-insensitive) and cut query candidates.

    This is the gate any planner, including a future LLM one, should pass its output
    through. It does not judge content; it only keeps the queries valid for ``SearchQuery``.
    """
    if isinstance(max_queries, bool) or not isinstance(max_queries, int):
        raise ValueError("max_queries must be an integer")
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if len(out) >= max(max_queries, 0):
            break
        if not isinstance(candidate, str):
            continue
        text = _clean_query(candidate)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def _language(text: str) -> str | None:
    if _HAS_KANA.search(text):
        return "ja"
    if _HAS_LATIN.search(text) and not _HAS_CJK.search(text):
        return "en"
    return None


def _recency_terms(text: str) -> list[str]:
    """The question's own recency words and years, in order of appearance."""
    found: list[tuple[int, str]] = []
    for match in _RECENCY_EN.finditer(text):
        found.append((match.start(), match.group()))
    for cue in _RECENCY_JA:
        index = text.find(cue)
        if index >= 0:
            found.append((index, cue))
    for match in _YEAR.finditer(text):
        found.append((match.start(), match.group()))
    ordered: list[str] = []
    for _, term in sorted(found):
        if term not in ordered:
            ordered.append(term)
    return ordered


def _chunks(text: str, *, drop_comparison: bool) -> list[str]:
    """Content terms of the question in order, without recency words and stopwords.

    ASCII words are trimmed of edge punctuation; Japanese text is split into katakana/kanji
    runs, so hiragana (mostly particles) separates terms. Recency words are removed here and
    appended again by the time-qualified query. With ``drop_comparison`` the words that only
    name the comparison itself (``vs``, ``compare``, ``違い``) are removed too.
    """
    text = _RECENCY_EN.sub(" ", text)
    for cue in _RECENCY_JA:
        text = text.replace(cue, " ")
    text = _YEAR.sub(" ", text)
    found: list[tuple[int, str]] = []
    for match in _ASCII_WORD.finditer(text):
        word = match.group().strip(".-_+#")
        folded = word.casefold()
        if (len(word) > 1 or word.isdigit()) and folded not in EN_STOPWORDS:
            if not (drop_comparison and folded in _COMPARISON_WORDS):
                found.append((match.start(), word))
    for match in _CJK_RUN.finditer(text):
        run = match.group()
        if len(run) > 1 and run not in _JA_STOP_CHUNKS:
            if not (drop_comparison and run in _JA_COMPARISON_CHUNKS):
                found.append((match.start(), run))
    terms: list[str] = []
    seen: set[str] = set()
    for _, term in sorted(found):
        if term.casefold() not in seen:
            seen.add(term.casefold())
            terms.append(term)
    return terms


def _trim_phrase(words: list[str], *, from_end: bool) -> list[str]:
    while words and (words[-1] if from_end else words[0]).casefold() in _PHRASE_STOP:
        words = words[:-1] if from_end else words[1:]
    return words


def _split_vs(text: str) -> list[str]:
    """Targets of ``A vs B [vs C]``: the neighbouring word of each ``vs``."""
    parts = _EN_VS.split(text)
    if len(parts) < 2:
        return []
    targets: list[str] = []
    for index, part in enumerate(parts):
        words = [w.strip(_WORD_EDGE) for w in part.split() if w.strip(_WORD_EDGE)]
        if not words:
            return []
        if index == 0:
            targets.append(words[-1])
        elif index == len(parts) - 1:
            targets.append(words[0])
        else:
            targets.append(" ".join(words[:3]))
    return targets


def _extract_targets(text: str) -> tuple[str, ...]:
    targets: list[str] = _split_vs(text)
    if not targets:
        for pattern in (_EN_BETWEEN, _EN_OR):
            match = pattern.search(text)
            if match:
                left = _trim_phrase(match.group("a").split(), from_end=True)
                right = _trim_phrase(match.group("b").split(), from_end=True)
                right = right[
                    : next((i for i, w in enumerate(right) if w.casefold() in _PHRASE_STOP), 3)
                ]
                targets = [" ".join(left), " ".join(right)]
                break
    if not targets:
        match = _JA_PAIR.search(text)
        if match:
            targets = [match.group("a"), match.group("b")]
    cleaned: list[str] = []
    for target in targets:
        target = target.strip(_WORD_EDGE + " ")[:MAX_TARGET_CHARS].strip()
        if (
            target
            and target.casefold() not in EN_STOPWORDS
            and target.casefold() not in {t.casefold() for t in cleaned}
            and target in text  # never a target the question does not contain
        ):
            cleaned.append(target)
    return tuple(cleaned[:MAX_TARGETS]) if len(cleaned) >= 2 else ()


def _join(terms: Iterable[str], limit: int) -> str:
    out = ""
    for term in terms:
        candidate = f"{out} {term}".strip()
        if len(candidate) > limit:
            break
        out = candidate
    return out


class DeterministicQueryPlanner:
    """Query planner v2: deterministic, offline, question-words only. See the module docstring."""

    def plan(self, question: str, max_queries: int) -> list[str]:
        return [query.text for query in self.plan_detailed(question, max_queries).queries]

    def plan_detailed(self, question: str, max_queries: int) -> QueryPlan:
        if not isinstance(question, str):
            raise ValueError("question must be a string")
        if isinstance(max_queries, bool) or not isinstance(max_queries, int):
            raise ValueError("max_queries must be an integer")
        text = clean_text(question)
        recency = _recency_terms(text)
        targets = _extract_targets(text)
        core = _chunks(text, drop_comparison=bool(targets))[:MAX_KEYWORD_TERMS]
        target_words = {w.casefold() for target in targets for w in target.split()}
        context = [t for t in core if t.casefold() not in target_words]

        candidates = [PlannedQuery(text, QueryKind.QUESTION)]
        candidates += [
            PlannedQuery(_join([target, *context], MAX_KEYWORD_QUERY_CHARS), QueryKind.TARGET)
            for target in targets
        ]
        if recency and core:
            joined = _join([*core, *recency], MAX_KEYWORD_QUERY_CHARS)
            candidates.append(PlannedQuery(joined, QueryKind.TIME))
        if core:
            candidates.append(
                PlannedQuery(_join(core, MAX_KEYWORD_QUERY_CHARS), QueryKind.KEYWORDS)
            )

        planned: list[PlannedQuery] = []
        seen: set[str] = set()
        for candidate in candidates:
            query = _clean_query(candidate.text)
            if len(planned) < max(max_queries, 0) and query and query.casefold() not in seen:
                seen.add(query.casefold())
                planned.append(PlannedQuery(query, candidate.kind))
        years_only = all(_YEAR.fullmatch(term) for term in recency)
        hints = QueryHints(
            language=_language(text),
            recency_terms=tuple(recency),
            suggested_recency_days=RECENCY_WINDOW_DAYS if recency and not years_only else None,
            comparison_targets=targets,
            keywords=tuple(core),
        )
        return QueryPlan(tuple(planned), hints)
