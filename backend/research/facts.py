"""Deterministic fact extraction for the cross-check (JAR-46) and conflict detection (JAR-47).

From a piece of text this module pulls out the few things two statements can visibly agree or
disagree about: numbers with their unit, dates, a negation flag, content terms and named
entities. Everything is pattern matching on the text; no model, no network, no dictionary.

It is a HEURISTIC. It reads surface forms, not meaning:

* a number is only compared with a number of the same unit (``5 gb`` against ``8 gb``);
  unitless numbers are compared with unitless numbers, which is noisy;
* a four digit number between 1900 and 2100 is a date only when a cue word comes first
  (``in 2024``, ``since 2020``) or ``年`` follows; otherwise it is a number;
* negation is the presence of a negation word in the sentence, so it misses implicit
  negation ("fails", "denies") and double negatives;
* entities are capitalised words, acronyms and katakana runs, so lower-case names and
  most Japanese names are missed.

Agreement here means "the surface facts line up", never "the claim is true". Input text is
untrusted: it is cleaned (``planner.clean_text``), bounded, and only ever matched against
fixed patterns; nothing in it is executed, fetched or copied into a code.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from backend.research.planner import EN_STOPWORDS, clean_text
from backend.research.terms import extract_terms

MAX_TEXT_CHARS = 20_000
MAX_SENTENCES = 200
MAX_SENTENCE_CHARS = 600
MAX_ITEMS_PER_SENTENCE = 32  # numbers, dates and entities kept per sentence
_MAX_DIGITS = 15

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")
_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_YEAR_RE = r"(1[5-9][0-9]{2}|20[0-9]{2}|21[0-9]{2})"

# Dates are removed from the sentence once read, so their digits are not also numbers.
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"(?<![0-9]){_YEAR_RE}[-/.]([0-9]{{1,2}})[-/.]([0-9]{{1,2}})(?![0-9])"), "ymd"),
    (re.compile(rf"(?<![0-9]){_YEAR_RE}年\s*([0-9]{{1,2}})月\s*([0-9]{{1,2}})日"), "ymd"),
    (re.compile(rf"(?<![0-9]){_YEAR_RE}年\s*([0-9]{{1,2}})月"), "ym"),
    (
        re.compile(
            rf"(?<![a-z0-9])({_MONTH_RE})\.?\s+([0-9]{{1,2}})(?:st|nd|rd|th)?,?\s+{_YEAR_RE}"
            r"(?![0-9])"
        ),
        "mdy",
    ),
    (
        re.compile(
            rf"(?<![0-9])([0-9]{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\.?,?\s+{_YEAR_RE}(?![0-9])"
        ),
        "dmy",
    ),
    (re.compile(rf"(?<![a-z0-9])({_MONTH_RE})\.?,?\s+{_YEAR_RE}(?![0-9])"), "my"),
    (re.compile(rf"(?<![0-9]){_YEAR_RE}年"), "y"),
    (
        re.compile(
            r"(?<![a-z0-9])(?:in|since|until|till|by|during|from|before|after|year|as of)\s+"
            rf"{_YEAR_RE}(?![0-9])"
        ),
        "y_cue",
    ),
)
_VERSION = re.compile(r"(?<![0-9a-z.])[0-9]+(?:\.[0-9]+){2,}(?![0-9.]*[0-9a-z])")
_NUMBER = re.compile(r"(?<![0-9a-z.,])([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(\.[0-9]+)?(?![0-9])")
_UNITS_EN = frozenset(
    "percent pct kb mb gb tb pb kib mib gib tib ms s sec secs second seconds min mins minute "
    "minutes hour hours day days week weeks month months year years users people items "
    "usd eur jpy gbp dollar dollars euro euros yen km m cm mm kg g mg lb lbs hz khz mhz ghz "
    "v w kw mw mah fps gpu cpu core cores threads nodes".split()
)
_UNITS_CJK = frozenset("円ドル人件個台回倍万億兆本冊匹社歳点位")
_UNIT_AFTER = re.compile(r"\s{0,1}(%|[a-z]{1,10}|[円ドル人件個台回倍万億兆本冊匹社歳点位])")
_NEGATION_EN = re.compile(
    r"(?<![a-z0-9])(?:not|no|never|none|nothing|neither|nor|cannot|without|unable|"
    r"[a-z]+n['’]t|fails? to|failed to)(?![a-z0-9])"
)
_NEGATION_JA = ("ない", "ません", "ではなく", "ではない", "じゃない", "不可", "非対応", "未対応")
_ENTITY = re.compile(r"\b(?:[A-Z][A-Za-z0-9]+|[A-Z]{2,})(?:[ -](?:[A-Z][A-Za-z0-9]+|[A-Z]{2,}))*\b")
_KATAKANA = re.compile(r"[ァ-ヶー]{3,}")
_NEGATION_TERMS = frozenset({"not", "no", "never", "none", "nothing", "neither", "nor", "cannot"})


@dataclass(frozen=True)
class Quantity:
    """A number in canonical form with its unit (``""`` when none was found)."""

    value: str
    unit: str = ""


@dataclass(frozen=True)
class Facts:
    """What one sentence (or one short claim) states on the surface."""

    numbers: frozenset[Quantity] = frozenset()
    dates: frozenset[str] = frozenset()  # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    negated: bool = False
    terms: frozenset[str] = frozenset()
    entities: frozenset[str] = frozenset()


def split_sentences(text: str) -> list[str]:
    """Cleaned sentences of at most ``MAX_SENTENCE_CHARS`` characters, at most ``MAX_SENTENCES``."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    cleaned = clean_text(text, MAX_TEXT_CHARS)
    out: list[str] = []
    for part in _SENTENCE_END.split(cleaned):
        part = part.strip()
        if part:
            out.append(part[:MAX_SENTENCE_CHARS])
            if len(out) >= MAX_SENTENCES:
                break
    return out


def normalise(text: str) -> str:
    """Case-folded, whitespace-collapsed form used for verbatim comparison."""
    return clean_text(text, MAX_TEXT_CHARS).casefold()


def _valid_day(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _date_values(kind: str, match: re.Match[str]) -> list[str]:
    groups = match.groups()
    if kind == "ymd":
        year, month, day = (int(g) for g in groups)
        return [f"{year:04d}-{month:02d}-{day:02d}"] if _valid_day(year, month, day) else []
    if kind == "ym":
        year, month = int(groups[0]), int(groups[1])
        return [f"{year:04d}-{month:02d}"] if 1 <= month <= 12 else []
    if kind == "mdy":
        month, day, year = _MONTHS[groups[0]], int(groups[1]), int(groups[2])
        return [f"{year:04d}-{month:02d}-{day:02d}"] if _valid_day(year, month, day) else []
    if kind == "dmy":
        day, month, year = int(groups[0]), _MONTHS[groups[1]], int(groups[2])
        return [f"{year:04d}-{month:02d}-{day:02d}"] if _valid_day(year, month, day) else []
    if kind == "my":
        return [f"{int(groups[1]):04d}-{_MONTHS[groups[0]]:02d}"]
    return [f"{int(groups[0]):04d}"]  # "y" and "y_cue"


def _canonical_number(whole: str, fraction: str | None) -> str | None:
    digits = whole.replace(",", "") + (fraction or "")
    if len(digits) > _MAX_DIGITS:
        return None
    try:
        return format(Decimal(digits).normalize(), "f")
    except InvalidOperation:
        return None


def _unit_after(text: str, end: int) -> str:
    match = _UNIT_AFTER.match(text, end)
    if match is None:
        return ""
    unit = match.group(1)
    if unit in {"%", "pct", "percent"}:
        return "percent"
    return unit if unit in _UNITS_CJK or unit in _UNITS_EN else ""


def dates_compatible(first: str, second: str) -> bool:
    """Two dates agree when one is a prefix of the other (``2024`` and ``2024-05-01``)."""
    return first.startswith(second) or second.startswith(first)


def _extract_quantities_and_dates(
    text: str,
) -> tuple[frozenset[Quantity], frozenset[str], str]:
    dates: list[str] = []
    masked = text
    for pattern, kind in _DATE_PATTERNS:

        def blank(match: re.Match[str], kind: str = kind) -> str:
            dates.extend(_date_values(kind, match))
            return " " * len(match.group())

        masked = pattern.sub(blank, masked)
    numbers: list[Quantity] = []
    for match in _VERSION.finditer(masked):
        numbers.append(Quantity(match.group(), "version"))
    masked = _VERSION.sub(lambda m: " " * len(m.group()), masked)
    for match in _NUMBER.finditer(masked):
        value = _canonical_number(match.group(1), match.group(2))
        if value is None:
            continue
        numbers.append(Quantity(value, _unit_after(masked, match.end())))
    return (
        frozenset(numbers[:MAX_ITEMS_PER_SENTENCE]),
        frozenset(dates[:MAX_ITEMS_PER_SENTENCE]),
        masked,
    )


def _is_negated(folded: str) -> bool:
    return bool(_NEGATION_EN.search(folded)) or any(cue in folded for cue in _NEGATION_JA)


def _entities(original: str) -> frozenset[str]:
    found: set[str] = set()
    for match in _ENTITY.finditer(original):
        name = match.group().casefold()
        words = name.replace("-", " ").split()
        # A single capitalised word that is a stop word is just a sentence opener.
        if len(words) == 1 and (
            words[0] in EN_STOPWORDS | _NEGATION_TERMS | _UNITS_EN or words[0] in _MONTHS
        ):
            continue
        found.add(name)
    found.update(match.group() for match in _KATAKANA.finditer(original))
    return frozenset(sorted(found)[:MAX_ITEMS_PER_SENTENCE])


def extract_facts(text: str, limit: int = MAX_SENTENCE_CHARS) -> Facts:
    """Facts of one sentence or short claim. Only the first ``limit`` characters are read."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TEXT_CHARS:
        raise ValueError("limit must be an integer from 1 to 20000")
    original = clean_text(text, limit)
    # NFKC was applied by clean_text; fold the case for everything but the entity scan.
    folded = original.casefold()
    numbers, dates, masked = _extract_quantities_and_dates(folded)
    terms, singles = extract_terms(masked)
    # Terms that carry a digit are numbers or dates, which are compared separately; negation
    # words are compared as the negation flag, so neither belongs in the subject.
    content = frozenset(
        term
        for term in terms | singles
        if not any(ch.isdigit() for ch in term) and term not in _NEGATION_TERMS
    )
    return Facts(
        numbers=numbers,
        dates=dates,
        negated=_is_negated(folded),
        terms=content,
        entities=_entities(unicodedata.normalize("NFKC", original)),
    )


def term_coverage(wanted: Facts, found: Facts) -> float:
    """Share of ``wanted``'s content terms that also occur in ``found`` (1.0 when none)."""
    if not wanted.terms:
        return 1.0
    return len(wanted.terms & found.terms) / len(wanted.terms)


def same_subject(first: Facts, second: Facts) -> bool:
    """Whether two statements are about the same thing, by shared terms and entities.

    Either at least half of the smaller term set is shared (and at least two terms, or one
    term when a side has only one), or a named entity is shared together with a term.
    """
    shared = first.terms & second.terms
    smaller = min(len(first.terms), len(second.terms))
    if smaller == 0 or not shared:
        return False
    if len(shared) >= min(2, smaller) and len(shared) / smaller >= 0.5:
        return True
    return bool((first.entities & second.entities) and shared)
