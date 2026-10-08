"""Conflict detection (JAR-47): statements from different sources that visibly disagree.

Two things are compared, both by surface facts (``facts.py``):

* two claims that cite different sources and are about the same subject (shared content
  terms or a shared named entity): a different figure with the same unit
  (``number_mismatch``), a different date (``date_mismatch``), or one negated and the other
  not (``negation_mismatch``);
* a claim against the text of another source (the cross-check stance ``contradicts`` of
  ``crosscheck.py``), recorded as a conflict between the claim and that source.

A conflict is a FLAG for a person, not a finding. The detector never decides which side is
right, never edits or drops a claim, and never closes a conflict: records are stored ``open``
and only ``ResearchRepository.resolve_conflict`` (an explicit call with a fixed resolution
code) closes one. A later step that writes an answer must show open conflicts.

Heuristic limits: a different year of the same statistic, a different unit spelling, a
quoted opposite view or a sentence with an unrelated "not" produce false positives, while a
paraphrase without shared words or an implicit contradiction is missed. The records carry only
ids and a fixed kind; no text from claims or pages is stored in them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from backend.research.crosscheck import (
    ClaimInput,
    EvidenceText,
    Stance,
    claim_inputs,
    cross_check,
)
from backend.research.facts import Facts, dates_compatible, extract_facts, same_subject
from backend.research.models import MAX_CLAIM_CHARS, ConflictKind, ResearchConflict
from backend.research.repository import ResearchRepository

_STRONG_SUBJECT = 0.5  # share of the joint terms for a negation to count as a flip


@dataclass(frozen=True)
class ConflictCandidate:
    """A detected disagreement, before it is stored. Exactly one of the two "other" ids is set."""

    kind: ConflictKind
    claim_id: UUID
    other_claim_id: UUID | None = None
    other_source_id: UUID | None = None


def compare_statements(first: Facts, second: Facts) -> tuple[ConflictKind, ...]:
    """The ways two statements about the same subject disagree (empty: none seen)."""
    if not same_subject(first, second):
        return ()
    kinds: list[ConflictKind] = []
    units = {q.unit for q in first.numbers} & {q.unit for q in second.numbers}
    for unit in sorted(units):
        left = {q.value for q in first.numbers if q.unit == unit}
        right = {q.value for q in second.numbers if q.unit == unit}
        if not left & right:
            kinds.append(ConflictKind.NUMBER_MISMATCH)
            break
    if (
        first.dates
        and second.dates
        and not any(dates_compatible(a, b) for a in first.dates for b in second.dates)
    ):
        kinds.append(ConflictKind.DATE_MISMATCH)
    joint = first.terms | second.terms
    if (
        first.negated != second.negated
        and joint
        and len(first.terms & second.terms) / len(joint) >= _STRONG_SUBJECT
    ):
        kinds.append(ConflictKind.NEGATION_MISMATCH)
    return tuple(kinds)


def detect_claim_conflicts(claims: Sequence[ClaimInput]) -> list[ConflictCandidate]:
    """Claim-against-claim conflicts between claims that cite different sources."""
    facts = [extract_facts(claim.text, MAX_CLAIM_CHARS) for claim in claims]
    found: list[ConflictCandidate] = []
    for i, first in enumerate(claims):
        for j in range(i + 1, len(claims)):
            second = claims[j]
            if first.source_id == second.source_id or first.claim_id == second.claim_id:
                continue
            found.extend(
                ConflictCandidate(kind, first.claim_id, other_claim_id=second.claim_id)
                for kind in compare_statements(facts[i], facts[j])
            )
    return found


def detect_conflicts(
    claims: Sequence[ClaimInput], evidence: Sequence[EvidenceText] = ()
) -> list[ConflictCandidate]:
    """All conflicts among ``claims`` and, when ``evidence`` is given, against source texts.

    A claim-against-source conflict is left out when the same disagreement is already
    reported between two claims. Pure and deterministic; bounds are those of ``cross_check``.
    """
    found = detect_claim_conflicts(claims)
    if not evidence:
        return found
    source_of = {claim.claim_id: claim.source_id for claim in claims}
    paired = {
        (c.claim_id, source_of[c.other_claim_id], c.kind)
        for c in found
        if c.other_claim_id is not None
    } | {
        (c.other_claim_id, source_of[c.claim_id], c.kind)
        for c in found
        if c.other_claim_id is not None
    }
    report = cross_check(claims, evidence)
    for outcome in report.claims:
        own_source = source_of[outcome.claim_id]
        for stance in outcome.stances:
            if stance.stance is not Stance.CONTRADICTS or stance.source_id == own_source:
                continue
            for kind in stance.mismatches:
                if (outcome.claim_id, stance.source_id, kind) not in paired:
                    found.append(
                        ConflictCandidate(kind, outcome.claim_id, other_source_id=stance.source_id)
                    )
    return found


def detect_and_store_conflicts(
    repository: ResearchRepository,
    session_id: UUID,
    texts: Mapping[UUID, str] | None = None,
) -> list[ResearchConflict]:
    """Detect conflicts among the stored claims of a session and record them as ``open``.

    ``texts`` (source id to page text, as in ``cross_check_and_store``) additionally checks
    each claim against the other sources' text. Recording is idempotent: a conflict that is
    already stored, open or resolved, is returned as it is and is never reopened. Existing
    conflicts that are no longer detected are left alone. Raises what ``add_conflict``
    raises, for example when the session is already final.
    """
    sources = {source.id for source in repository.list_sources(session_id)}
    evidence = [
        EvidenceText(source_id, text)
        for source_id, text in (texts or {}).items()
        if source_id in sources and isinstance(text, str)
    ]
    claims = claim_inputs(repository.list_claims(session_id))
    return [
        repository.add_conflict(
            session_id,
            candidate.kind,
            candidate.claim_id,
            other_claim_id=candidate.other_claim_id,
            other_source_id=candidate.other_source_id,
        )
        for candidate in detect_conflicts(claims, evidence)
    ]
