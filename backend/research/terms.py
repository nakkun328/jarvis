"""Lexical term extraction shared by the relevance rating and the fact checks.

Text is expected to be already cleaned and case-folded (``planner.clean_text(...).casefold()``).
English is split into lower-case words (stop words removed, a trailing plural ``s`` dropped);
Japanese is split at common particles and cut into character bigrams, so it needs no
dictionary. This is a cheap lexical baseline: it has no notion of synonyms or meaning.
"""

import re
from collections.abc import Iterable

from backend.research.planner import EN_STOPWORDS, clean_text

_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9._+#\-]*")
_CJK_RUN = re.compile(r"[ぁ-んァ-ヶー一-龥々]+")
_PARTICLE_SPLIT = re.compile("[はがをにへでとのもやかねよ]+")
_HIRAGANA = re.compile(r"[ぁ-ん]")
_COMPOUND_SPLIT = re.compile(r"[._+#\-]+")


def normalize_latin(word: str) -> str:
    """Case-folded ASCII word without edge punctuation and without a plural ``s``."""
    word = word.casefold().strip(".-_+#")
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def latin_terms(text: str, *, split_compounds: bool = False) -> set[str]:
    """ASCII technical tokens (len >= 2 or a digit, not stop words) of case-folded ``text``.

    With ``split_compounds`` a token such as ``wal-index`` or ``sqlite.org`` also contributes
    its parts (``wal``, ``index``, ``sqlite``, ``org``); this is meant for page text and URLs,
    not for the question, so that a page mentioning ``WAL-index`` still matches ``WAL``.
    """
    terms: set[str] = set()
    for match in _ASCII_TERM.finditer(text):
        raw = match.group()
        parts = [raw]
        if split_compounds:
            parts += [part for part in _COMPOUND_SPLIT.split(raw) if part]
        for part in parts:
            word = normalize_latin(part)
            if (len(word) > 1 or word.isdigit()) and word not in EN_STOPWORDS:
                terms.add(word)
    return terms


def extract_terms(text: str) -> tuple[set[str], set[str]]:
    """(matchable terms, single-character terms that are matched as substrings)."""
    terms: set[str] = latin_terms(text)
    singles: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for segment in _PARTICLE_SPLIT.split(run):
            if len(segment) == 1:
                if not _HIRAGANA.fullmatch(segment):
                    singles.add(segment)
                continue
            for index in range(len(segment) - 1):
                pair = segment[index : index + 2]
                if not all(_HIRAGANA.fullmatch(ch) for ch in pair):
                    terms.add(pair)
    return terms, singles


MAX_QUERIES_USED = 32
_QUERY_CHARS = 500


def _folded(text: str, limit: int) -> str:
    return clean_text(text, limit).casefold()


def question_latin_terms(question: str, limit: int = 2000) -> set[str]:
    """The Latin/ASCII technical terms the asker wrote (the most distinctive terms)."""
    return latin_terms(_folded(question, limit))


def query_latin_terms(queries: Iterable[str]) -> tuple[set[str], set[str]]:
    """(every Latin term of the issued queries, those every query has; the latter needs 2+ queries).

    The planner repeats the subject in every query while the generic words vary, so a term
    that all the queries share is a better subject candidate than one used once.
    """
    per_query = [
        latin_terms(_folded(query, _QUERY_CHARS))
        for query in list(queries)[:MAX_QUERIES_USED]
        if isinstance(query, str)
    ]
    every: set[str] = set().union(*per_query) if per_query else set()
    if len(per_query) < 2:
        return every, set()
    shared = {
        term
        for term in every
        if all(term in terms for terms in per_query)
    }
    return every, shared


def subject_terms(question: str, queries: Iterable[str] = ()) -> frozenset[str]:
    """Latin terms that name what the question is about: the question's own plus shared ones."""
    _every, shared = query_latin_terms(queries)
    return frozenset(question_latin_terms(question) | shared)
