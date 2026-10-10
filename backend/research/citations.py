"""Citation manager: only claims whose quotes really occur in their source are kept.

An LLM proposes claims as (text, source number, quote). Nothing it says is trusted:
a claim survives only when its source number names an evidence source of this run
and its quote is a verbatim, whitespace-normalised substring of THAT source's
extracted text. Surviving claims are the only ones persisted, and the rendered
citation list is built from stored source records, never from model output.

Quote offsets are character offsets into the in-memory extracted page text of the run
(the text is not stored). The stored quote is the whitespace-normalised form, so
``quote_end - quote_start`` can exceed ``len(quote)`` when the page spacing differs.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.research.models import (
    MAX_CLAIM_CHARS,
    MAX_QUOTE_CHARS,
    ResearchClaim,
    ResearchSource,
)
from backend.research.planner import EN_STOPWORDS
from backend.research.repository import ResearchRepository

MIN_QUOTE_CHARS = 8
_URL = re.compile(r"https?://[^\s<>\"'\])}]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?"
_LINK_REMOVED = "[link removed]"
NEAR_DUPLICATE_AT = 0.7
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
_WORD = re.compile(r"[^\W\d_]+")
_NEGATIONS = frozenset({"not", "no", "never", "cannot", "without", "ない", "ません", "不"})


# A "claim" that only says information is missing is not a finding. Japanese patterns match
# the end of the statement; English patterns that could also state a real fact ("not
# available in region X") need a word about the supplied material in the same statement.
_ABSENCE_JA = re.compile(
    r"(?:確認でき(?:ません|ない|ず)|記載(?:され)?(?:て)?(?:い)?(?:ません|ない|がありません|はありません)"
    r"|明記され(?:て)?(?:い)?(?:ません|ない)|言及され(?:て)?(?:い)?(?:ません|ない)"
    r"|示され(?:て)?(?:い)?(?:ません|ない)|見つかりません|見当たりません|含まれ(?:て)?(?:い)?(?:ません|ない)"
    r"|情報(?:は|が)(?:ありません|ない)|不明(?:です|である|だ)?|特定でき(?:ません|ない))"
    r"[。.!！\s]*$"
)
_ABSENCE_EN_STRONG = re.compile(
    r"\b(?:cannot|can ?not|could not|couldn't|unable to|can't)\s+(?:be\s+)?"
    r"(?:confirm|confirmed|verify|verified|determine|determined|find|found|identify|identified)\b"
    r"|\bnot\s+(?:been\s+)?(?:mentioned|stated|specified|disclosed)\b"
)
_ABSENCE_EN_WEAK = re.compile(
    r"\bnot (?:provided|available|listed|given|included|found|clear)\b|\bunclear\b"
    r"|\bno (?:information|data|details|mention)\b"
    r"|\bdoes not (?:mention|state|specify|say|provide)\b"
)
_MATERIAL_EN = re.compile(
    r"\b(?:provided|given|supplied|available|supplied)\s+(?:materials?|sources?|documents?|"
    r"texts?|pages?|information|content)\b|\b(?:sources?|materials?|documents?|pages?|texts?|"
    r"excerpts?|context)\b"
)


def is_absence_statement(text: str) -> bool:
    """True when the statement only says that information is missing or cannot be confirmed.

    Deterministic surface patterns in Japanese and English; the text is NFKC-normalised first.
    Anything else, including a negative statement of fact, is not an absence statement.
    """
    folded = unicodedata.normalize("NFKC", text).casefold().strip()
    if not folded:
        return False
    if _ABSENCE_JA.search(folded):
        return True
    if _ABSENCE_EN_STRONG.search(folded):
        return True
    return bool(_ABSENCE_EN_WEAK.search(folded) and _MATERIAL_EN.search(folded))


class DropReason(StrEnum):
    """Fixed codes for claims that were not kept."""

    MALFORMED = "malformed"
    UNKNOWN_SOURCE = "unknown_source"
    QUOTE_TOO_SHORT = "quote_too_short"
    QUOTE_TOO_LONG = "quote_too_long"
    QUOTE_NOT_FOUND = "quote_not_found"
    CLAIM_TOO_LONG = "claim_too_long"
    DUPLICATE = "duplicate"
    OVER_LIMIT = "over_limit"
    NEAR_DUPLICATE = "near_duplicate"
    ABSENCE_STATEMENT = "absence_statement"


@dataclass(frozen=True)
class EvidenceSource:
    """A source the run actually read: its 1-based number, stored record and page text."""

    index: int
    source: ResearchSource
    text: str


@dataclass(frozen=True)
class ProposedClaim:
    text: str
    source: int
    quote: str


@dataclass(frozen=True)
class VerifiedClaim:
    text: str
    source_index: int
    source_id: UUID
    quote: str  # whitespace-normalised
    quote_start: int
    quote_end: int


@dataclass(frozen=True)
class DroppedClaim:
    reason: DropReason
    source: int | None = None


@dataclass(frozen=True)
class VerificationReport:
    verified: tuple[VerifiedClaim, ...]
    dropped: tuple[DroppedClaim, ...]


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def _signature(text: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """(content tokens, numbers, negated) of a claim; words are case-folded, CJK uses bigrams."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    numbers = frozenset(m.replace(",", "") for m in _NUMBER.findall(folded))
    tokens: set[str] = set()
    for run in _CJK_RUN.findall(folded):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    plain = _CJK_RUN.sub(" ", _NUMBER.sub(" ", folded))
    words = [w for w in _WORD.findall(plain) if w not in EN_STOPWORDS]
    tokens.update(words)
    negated = bool(_NEGATIONS & set(words)) or "n't" in folded or any(
        n in folded for n in ("ない", "ません", "不")
    )
    return frozenset(tokens), numbers, negated


def near_duplicate(first: str, second: str) -> bool:
    """True for two wordings of one statement: same numbers and negation, most words shared.

    Deterministic and surface-only: lower-case word (or CJK bigram) Jaccard of at least
    ``NEAR_DUPLICATE_AT`` after dropping stop words, with identical numbers and negation.
    """
    tokens_a, numbers_a, negated_a = _signature(first)
    tokens_b, numbers_b, negated_b = _signature(second)
    if numbers_a != numbers_b or negated_a != negated_b:
        return False
    union = tokens_a | tokens_b
    if not union:
        return False
    return len(tokens_a & tokens_b) / len(union) >= NEAR_DUPLICATE_AT


def find_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Offsets of the first verbatim occurrence of ``quote``; whitespace runs may differ."""
    tokens = quote.split()
    if not tokens:
        return None
    match = re.search(r"\s+".join(re.escape(token) for token in tokens), text)
    return (match.start(), match.end()) if match else None


class CitationManager:
    def __init__(
        self,
        repository: ResearchRepository,
        session_id: UUID,
        evidence: Sequence[EvidenceSource],
    ) -> None:
        self._repository = repository
        self._session_id = session_id
        self._evidence = {item.index: item for item in evidence}

    def verify(self, proposed: Sequence[ProposedClaim], *, max_claims: int) -> VerificationReport:
        verified: list[VerifiedClaim] = []
        dropped: list[DroppedClaim] = []
        seen: set[tuple[str, int, str]] = set()
        for claim in proposed:
            if len(verified) >= max_claims:
                dropped.append(DroppedClaim(DropReason.OVER_LIMIT, _source_ref(claim.source)))
                continue
            outcome = self._verify_one(claim)
            if isinstance(outcome, DropReason):
                dropped.append(DroppedClaim(outcome, _source_ref(claim.source)))
                continue
            key = (outcome.text, outcome.source_index, outcome.quote)
            if key in seen:
                dropped.append(DroppedClaim(DropReason.DUPLICATE, outcome.source_index))
                continue
            twin = _twin(verified, outcome)
            if twin is not None:
                # A reworded copy of a claim of the same source: keep the better-cited one.
                if len(outcome.quote) > len(verified[twin].quote):
                    seen.discard(
                        (verified[twin].text, verified[twin].source_index, verified[twin].quote)
                    )
                    verified[twin] = outcome
                    seen.add(key)
                dropped.append(DroppedClaim(DropReason.NEAR_DUPLICATE, outcome.source_index))
                continue
            seen.add(key)
            verified.append(outcome)
        return VerificationReport(tuple(verified), tuple(dropped))

    def _verify_one(self, claim: ProposedClaim) -> VerifiedClaim | DropReason:
        if (
            not isinstance(claim.text, str)
            or not isinstance(claim.quote, str)
            or isinstance(claim.source, bool)
            or not isinstance(claim.source, int)
        ):
            return DropReason.MALFORMED
        text = normalize_space(claim.text)
        if not text:
            return DropReason.MALFORMED
        if len(text) > MAX_CLAIM_CHARS:
            return DropReason.CLAIM_TOO_LONG
        evidence = self._evidence.get(claim.source)
        if evidence is None:
            return DropReason.UNKNOWN_SOURCE
        if is_absence_statement(text):
            return DropReason.ABSENCE_STATEMENT
        quote = normalize_space(claim.quote)
        if len(quote) < MIN_QUOTE_CHARS:
            return DropReason.QUOTE_TOO_SHORT
        if len(quote) > MAX_QUOTE_CHARS:
            return DropReason.QUOTE_TOO_LONG
        span = find_quote(evidence.text, quote)
        if span is None:
            return DropReason.QUOTE_NOT_FOUND
        return VerifiedClaim(text, evidence.index, evidence.source.id, quote, span[0], span[1])

    def persist(self, claims: Sequence[VerifiedClaim]) -> tuple[ResearchClaim, ...]:
        """Store verified claims; one already stored for the session is returned, not repeated."""
        existing = {
            (c.claim_text, c.source_id, c.quote): c
            for c in self._repository.list_claims(self._session_id)
        }
        stored: list[ResearchClaim] = []
        for claim in claims:
            key = (claim.text, claim.source_id, claim.quote)
            if key in existing:
                stored.append(existing[key])
                continue
            stored.append(
                self._repository.add_claim(
                    self._session_id,
                    claim_text=claim.text,
                    source_id=claim.source_id,
                    quote=claim.quote,
                    quote_start=claim.quote_start,
                    quote_end=claim.quote_end,
                )
            )
        return tuple(stored)

    def render(
        self,
        header: str,
        claims: Sequence[VerifiedClaim],
        *,
        dropped: int = 0,
        also_cited: Iterable[int] = (),
    ) -> str:
        """Final text: a fixed header, the verified claims, and a source list from stored records.

        Only verified claims are shown. ``header`` must be fixed text of ours, never the model's
        free-text answer (callers pass a constant); URLs in it are still checked.
        ``also_cited`` are source numbers that other parts of the final text refer to (for
        example open conflicts); they are listed under ``Sources:`` too, with the same numbers.
        """
        extra = {i for i in also_cited if i in self._evidence}
        allowed = {
            url
            for item in self._evidence.values()
            for url in (item.source.url, item.source.final_url)
        }
        lines = [strip_unknown_urls(header.strip(), allowed)]
        if claims:
            lines += ["", "Verified claims:"]
            lines += [
                f"- {strip_unknown_urls(claim.text, allowed)} [{claim.source_index}]"
                for claim in claims
            ]
        else:
            lines += ["", "No claim could be verified against a source."]
        listed = sorted({claim.source_index for claim in claims} | extra)
        if listed:
            lines += ["", "Sources:"]
            lines += [_source_line(index, self._evidence[index].source) for index in listed]
        if dropped:
            lines += [
                "",
                f"Note: {dropped} proposed claim(s) were removed because their sources or "
                "quotes could not be verified.",
            ]
        return "\n".join(lines)


def strip_unknown_urls(text: str, allowed: set[str]) -> str:
    """Replace any URL that is not one of the stored source URLs; models may not invent links."""

    def replace(match: re.Match[str]) -> str:
        url = match.group(0)
        trimmed = url.rstrip(_URL_TRAILING)
        tail = url[len(trimmed) :]
        if trimmed in allowed or trimmed.rstrip("/") in {a.rstrip("/") for a in allowed}:
            return url
        return _LINK_REMOVED + tail

    return _URL.sub(replace, text)


def _twin(verified: Sequence[VerifiedClaim], claim: VerifiedClaim) -> int | None:
    """Index of a kept claim of the same source that says the same thing, if any."""
    for position, kept in enumerate(verified):
        if kept.source_index == claim.source_index and near_duplicate(kept.text, claim.text):
            return position
    return None


def has_twin(verified: Sequence[VerifiedClaim], claim: VerifiedClaim) -> bool:
    """True when ``claim`` repeats (or rewords) a claim of the same source in ``verified``."""
    return _twin(verified, claim) is not None


def _source_ref(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _source_line(index: int, source: ResearchSource) -> str:
    title = source.title or "(untitled)"
    parts = [f"retrieved {source.retrieved_at.date().isoformat()}"]
    if source.published_at is not None:
        parts.append(f"published {source.published_at.date().isoformat()}")
    return f"[{index}] {title} - {source.final_url} ({'; '.join(parts)})"
