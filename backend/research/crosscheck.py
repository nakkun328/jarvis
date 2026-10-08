"""Cross-check (JAR-46): how well several sources agree with the claims, by surface facts.

For every claim the text of each source that was read is searched for the sentence that
talks about the same thing (shared content terms). That sentence, or the verbatim claim
quote, gives the source a ``Stance`` towards the claim:

* ``supports``: the claim quote occurs verbatim in the text, or the best sentence carries the
  claim's numbers and dates and names its entities;
* ``partial``: the sentence is on the subject but some of the claim's numbers or dates are
  missing from it;
* ``contradicts``: the sentence is on the subject but has a different number (same unit), a
  different date, or the opposite negation;
* ``silent``: nothing on the subject.

The ``agreement`` rating of a source is then the mean, over the claims it speaks to, of the
share of OTHER speaking sources that took the same side (``supports``/``partial`` against
``contradicts``). A source that never shares a claim with another source has no comparison:
its rating is ``None`` with ``agreement_no_comparison``. Ratings are stored with fixed reason
codes (``models.RatingReason``); no page text is copied into them.

Limits, stated plainly. This is a surface comparison, not understanding: agreement of two
pages can come from one copying the other (it counts as two sources), disagreement can be a
different unit or a different year of the same figure, and a paraphrase without the shared
words is invisible (the stance is ``silent``). The numbers say how the surface facts line up,
never that a claim is true. Page text is not stored, so the caller that read the pages passes
it in; only the first 20,000 characters of each are used.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from backend.research.facts import (
    Facts,
    dates_compatible,
    extract_facts,
    normalise,
    split_sentences,
    term_coverage,
)
from backend.research.models import (
    MAX_CLAIM_CHARS,
    ConflictKind,
    RatingName,
    RatingReason,
    ResearchClaim,
)
from backend.research.repository import ResearchRepository

MAX_CLAIMS = 100
MAX_EVIDENCE = 40
MIN_SENTENCE_COVERAGE = 0.5
MIN_QUOTE_CHARS = 8  # a shorter quote is too easy to find by accident
_DIGITS = 4
_CORROBORATED_AT = 0.75
_CONTRADICTED_AT = 0.25


class Stance(StrEnum):
    SUPPORTS = "supports"
    PARTIAL = "partial"
    CONTRADICTS = "contradicts"
    SILENT = "silent"


@dataclass(frozen=True)
class EvidenceText:
    """The text a source was read as. ``source_id`` is any hashable id, normally a UUID."""

    source_id: UUID
    text: str


@dataclass(frozen=True)
class ClaimInput:
    claim_id: UUID
    source_id: UUID
    text: str
    quote: str = ""


@dataclass(frozen=True)
class StanceResult:
    source_id: UUID
    stance: Stance
    verbatim: bool = False
    mismatches: tuple[ConflictKind, ...] = ()


@dataclass(frozen=True)
class ClaimCrossCheck:
    claim_id: UUID
    stances: tuple[StanceResult, ...]
    rating: float | None  # share agreeing among speaking sources; None below two speakers

    def speaking(self) -> tuple[StanceResult, ...]:
        return tuple(s for s in self.stances if s.stance is not Stance.SILENT)


@dataclass(frozen=True)
class SourceAgreement:
    source_id: UUID
    rating: float | None
    reasons: tuple[RatingReason, ...]


@dataclass(frozen=True)
class CrossCheckReport:
    claims: tuple[ClaimCrossCheck, ...]
    sources: tuple[SourceAgreement, ...]


@dataclass(frozen=True)
class _Prepared:
    source_id: UUID
    normalised: str
    sentences: tuple[Facts, ...]


def _prepare(evidence: EvidenceText) -> _Prepared:
    return _Prepared(
        evidence.source_id,
        normalise(evidence.text),
        tuple(extract_facts(sentence) for sentence in split_sentences(evidence.text)),
    )


def _best_sentence(claim: Facts, prepared: _Prepared) -> Facts | None:
    """The sentence sharing the most of the claim's content terms, if enough are shared."""
    best: Facts | None = None
    best_coverage = 0.0
    for sentence in prepared.sentences:
        coverage = term_coverage(claim, sentence)
        if coverage > best_coverage:
            best, best_coverage = sentence, coverage
    if best is None or best_coverage < MIN_SENTENCE_COVERAGE:
        return None
    if claim.terms and len(claim.terms & best.terms) < min(2, len(claim.terms)):
        return None
    return best


def _compare(claim: Facts, sentence: Facts) -> tuple[Stance, tuple[ConflictKind, ...]]:
    mismatches: list[ConflictKind] = []
    matched = 0
    missing = 0
    sentence_units = {quantity.unit for quantity in sentence.numbers}
    for quantity in claim.numbers:
        if quantity in sentence.numbers:
            matched += 1
        elif quantity.unit in sentence_units:
            # Same unit, different figure. (Unitless numbers are compared with unitless only.)
            if ConflictKind.NUMBER_MISMATCH not in mismatches:
                mismatches.append(ConflictKind.NUMBER_MISMATCH)
        else:
            missing += 1
    for date_text in claim.dates:
        if any(dates_compatible(date_text, other) for other in sentence.dates):
            matched += 1
        elif sentence.dates:
            if ConflictKind.DATE_MISMATCH not in mismatches:
                mismatches.append(ConflictKind.DATE_MISMATCH)
        else:
            missing += 1
    if claim.negated != sentence.negated:
        mismatches.append(ConflictKind.NEGATION_MISMATCH)
    if mismatches:
        return Stance.CONTRADICTS, tuple(mismatches)
    entities_ok = (
        not claim.entities
        or bool(claim.entities & sentence.entities)
        or any(entity in sentence.terms for entity in claim.entities)
    )
    if missing == 0 and entities_ok:
        return Stance.SUPPORTS, ()
    return Stance.PARTIAL, ()


def _stance(claim: ClaimInput, facts: Facts, prepared: _Prepared) -> StanceResult:
    needles = [normalise(claim.quote), normalise(claim.text)]
    if any(len(needle) >= MIN_QUOTE_CHARS and needle in prepared.normalised for needle in needles):
        return StanceResult(prepared.source_id, Stance.SUPPORTS, verbatim=True)
    sentence = _best_sentence(facts, prepared)
    if sentence is None:
        return StanceResult(prepared.source_id, Stance.SILENT)
    stance, mismatches = _compare(facts, sentence)
    return StanceResult(prepared.source_id, stance, mismatches=mismatches)


def _positive(stance: Stance) -> bool:
    return stance in {Stance.SUPPORTS, Stance.PARTIAL}


def _claim_rating(speaking: Sequence[StanceResult]) -> float | None:
    if len(speaking) < 2:
        return None
    weight = {Stance.SUPPORTS: 1.0, Stance.PARTIAL: 0.5, Stance.CONTRADICTS: 0.0}
    return round(sum(weight[r.stance] for r in speaking) / len(speaking), _DIGITS)


_MISMATCH_REASON = {
    ConflictKind.NUMBER_MISMATCH: RatingReason.AGREEMENT_NUMBER_MISMATCH,
    ConflictKind.DATE_MISMATCH: RatingReason.AGREEMENT_DATE_MISMATCH,
    ConflictKind.NEGATION_MISMATCH: RatingReason.AGREEMENT_NEGATION_MISMATCH,
}


def cross_check(claims: Sequence[ClaimInput], evidence: Sequence[EvidenceText]) -> CrossCheckReport:
    """Stances per claim and the ``agreement`` rating per source. Pure and deterministic.

    Every source in ``evidence`` is compared with every claim, including the source that
    each claim cites. Raises ``ValueError`` for more than ``MAX_CLAIMS`` claims or
    ``MAX_EVIDENCE`` sources, duplicate source ids, or malformed items.
    """
    if len(claims) > MAX_CLAIMS or len(evidence) > MAX_EVIDENCE:
        raise ValueError("too many claims or sources to cross-check")
    ids = [item.source_id for item in evidence]
    if len(set(ids)) != len(ids):
        raise ValueError("source ids must be unique")
    for item in evidence:
        if not isinstance(item, EvidenceText) or not isinstance(item.text, str):
            raise ValueError("evidence must be EvidenceText items")
    for claim in claims:
        if (
            not isinstance(claim, ClaimInput)
            or not isinstance(claim.text, str)
            or not isinstance(claim.quote, str)
        ):
            raise ValueError("claims must be ClaimInput items")
    prepared = [_prepare(item) for item in evidence]
    results: list[ClaimCrossCheck] = []
    for claim in claims:
        facts = extract_facts(claim.text, MAX_CLAIM_CHARS)
        stances = tuple(_stance(claim, facts, item) for item in prepared)
        speaking = [s for s in stances if s.stance is not Stance.SILENT]
        results.append(ClaimCrossCheck(claim.claim_id, stances, _claim_rating(speaking)))

    sources: list[SourceAgreement] = []
    for item in prepared:
        shares: list[float] = []
        verbatim = False
        mismatch_kinds: list[ConflictKind] = []
        for outcome in results:
            mine = next(s for s in outcome.stances if s.source_id == item.source_id)
            if mine.stance is Stance.SILENT:
                continue
            verbatim = verbatim or mine.verbatim
            others = [
                s
                for s in outcome.stances
                if s.source_id != item.source_id and s.stance is not Stance.SILENT
            ]
            if not others:
                continue
            same = sum(_positive(o.stance) == _positive(mine.stance) for o in others)
            shares.append(same / len(others))
            for stance in (mine, *others):
                for kind in stance.mismatches:
                    if kind not in mismatch_kinds:
                        mismatch_kinds.append(kind)
        sources.append(_source_agreement(item.source_id, shares, verbatim, mismatch_kinds))
    return CrossCheckReport(tuple(results), tuple(sources))


def _source_agreement(
    source_id: UUID,
    shares: Sequence[float],
    verbatim: bool,
    mismatch_kinds: Sequence[ConflictKind],
) -> SourceAgreement:
    reasons: list[RatingReason] = []
    if not shares:
        reasons.append(RatingReason.AGREEMENT_NO_COMPARISON)
        if verbatim:
            reasons.append(RatingReason.AGREEMENT_VERBATIM_SUPPORT)
        return SourceAgreement(source_id, None, tuple(reasons))
    rating = round(sum(shares) / len(shares), _DIGITS)
    if rating >= _CORROBORATED_AT:
        reasons.append(RatingReason.AGREEMENT_CORROBORATED)
    elif rating <= _CONTRADICTED_AT:
        reasons.append(RatingReason.AGREEMENT_CONTRADICTED)
    else:
        reasons.append(RatingReason.AGREEMENT_MIXED)
    if verbatim:
        reasons.append(RatingReason.AGREEMENT_VERBATIM_SUPPORT)
    for kind in mismatch_kinds:
        reasons.append(_MISMATCH_REASON[kind])
    return SourceAgreement(source_id, rating, tuple(reasons[:4]))


def cross_check_and_store(
    repository: ResearchRepository,
    session_id: UUID,
    texts: Mapping[UUID, str],
) -> CrossCheckReport:
    """Cross-check the stored claims of a session and save each source's agreement rating.

    ``texts`` maps source ids of the session to the page text the caller read; sources
    without text, and ids that are not sources of this session, are ignored. Only
    ``agreement`` and its reason codes are written: every other rating is kept. Raises what
    ``set_evaluation`` raises, for example when the session is already final.
    """
    sources = {source.id: source for source in repository.list_sources(session_id)}
    evidence = [
        EvidenceText(source_id, text)
        for source_id, text in texts.items()
        if source_id in sources and isinstance(text, str)
    ]
    claims = [
        ClaimInput(claim.id, claim.source_id, claim.claim_text, claim.quote)
        for claim in repository.list_claims(session_id)
    ]
    report = cross_check(claims, evidence)
    for agreement in report.sources:
        current = sources[agreement.source_id].evaluation
        repository.set_evaluation(
            agreement.source_id,
            replace(current, agreement=agreement.rating),
            reasons={RatingName.AGREEMENT: agreement.reasons},
        )
    return report


def claim_inputs(claims: Sequence[ResearchClaim]) -> list[ClaimInput]:
    """``ClaimInput`` items for stored claims."""
    return [ClaimInput(c.id, c.source_id, c.claim_text, c.quote) for c in claims]
