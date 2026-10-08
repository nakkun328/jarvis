"""Lexical term extraction shared by the relevance rating and the fact checks.

Text is expected to be already cleaned and case-folded (``planner.clean_text(...).casefold()``).
English is split into lower-case words (stop words removed, a trailing plural ``s`` dropped);
Japanese is split at common particles and cut into character bigrams, so it needs no
dictionary. This is a cheap lexical baseline: it has no notion of synonyms or meaning.
"""

import re

from backend.research.planner import EN_STOPWORDS

_ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9._+#\-]*")
_CJK_RUN = re.compile(r"[ぁ-んァ-ヶー一-龥々]+")
_PARTICLE_SPLIT = re.compile("[はがをにへでとのもやかねよ]+")
_HIRAGANA = re.compile(r"[ぁ-ん]")


def extract_terms(text: str) -> tuple[set[str], set[str]]:
    """(matchable terms, single-character terms that are matched as substrings)."""
    terms: set[str] = set()
    singles: set[str] = set()
    for match in _ASCII_TERM.finditer(text):
        word = match.group().strip(".-_+#")
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if (len(word) > 1 or word.isdigit()) and word not in EN_STOPWORDS:
            terms.add(word)
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
