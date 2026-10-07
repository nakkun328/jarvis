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
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from backend.research.models import (
    MAX_CLAIM_CHARS,
    MAX_QUOTE_CHARS,
    ResearchClaim,
    ResearchSource,
)
from backend.research.repository import ResearchRepository

MIN_QUOTE_CHARS = 8
_URL = re.compile(r"https?://[^\s<>\"'\])}]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?"
_LINK_REMOVED = "[link removed]"


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

    def render(self, answer: str, claims: Sequence[VerifiedClaim], *, dropped: int = 0) -> str:
        """Final text: the answer, the verified claims, and a source list from stored records."""
        allowed = {
            url
            for item in self._evidence.values()
            for url in (item.source.url, item.source.final_url)
        }
        lines = [strip_unknown_urls(answer.strip(), allowed)]
        if claims:
            lines += ["", "Verified claims:"]
            lines += [
                f"- {strip_unknown_urls(claim.text, allowed)} [{claim.source_index}]"
                for claim in claims
            ]
            lines += ["", "Sources:"]
            for index in sorted({claim.source_index for claim in claims}):
                lines.append(_source_line(index, self._evidence[index].source))
        else:
            lines += ["", "No claim could be verified against a source."]
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


def _source_ref(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _source_line(index: int, source: ResearchSource) -> str:
    title = source.title or "(untitled)"
    parts = [f"retrieved {source.retrieved_at.date().isoformat()}"]
    if source.published_at is not None:
        parts.append(f"published {source.published_at.date().isoformat()}")
    return f"[{index}] {title} - {source.final_url} ({'; '.join(parts)})"
