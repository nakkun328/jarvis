"""Turn the verified claims of a completed research session into pending memory candidates.

Research output never becomes approved memory by itself. This module only stages candidates
through ``MemoryRepository.add`` (status ``pending``); approving one is a separate human review
step that publishes through ``MemoryWriter``. Nothing here writes to the vault. It is called only
from the API handler that a person triggers with a button; no model, tool or agent path calls it.

Only stored claims are used: each already carries a quote and a source. The free-text result and
any unverified text are never read.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.memory.model import MemoryCategory, MemoryOrigin, MemoryRecord
from backend.memory.repository import (
    MemoryAlreadyExists,
    MemoryRepository,
    MemoryRepositoryError,
    StoredMemory,
)
from backend.research.models import ConflictStatus, ResearchClaim, ResearchSource, ResearchStatus
from backend.research.repository import ResearchRepository

#: Most candidates created from one session, so one click cannot flood the review queue.
MAX_CANDIDATES_PER_SESSION = 5
RESEARCH_TAG = "research"
SOURCE_PREFIX = "research:"
#: Research text is third-party content: modest importance and confidence until a person reviews.
IMPORTANCE = 0.5
CONFIDENCE = 0.6
_NAMESPACE = uuid5(NAMESPACE_URL, "jarvis:research-memory-candidate")


class CandidateRefusal(StrEnum):
    """Fixed codes; the HTTP layer maps them to statuses."""

    SESSION_NOT_FOUND = "session_not_found"
    SESSION_NOT_COMPLETED = "session_not_completed"
    NO_VERIFIED_CLAIMS = "no_verified_claims"


class RefusedCandidates(Exception):
    def __init__(self, code: CandidateRefusal) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[StoredMemory, ...]
    created: int
    #: Verified claims beyond the cap, not staged.
    omitted: int
    #: Whether the session is completed and has at least one verified claim.
    eligible: bool = True


def candidate_id(claim_id: UUID) -> UUID:
    """The same claim always maps to the same candidate id, which makes staging idempotent."""
    return uuid5(_NAMESPACE, str(claim_id))


def research_source(session_id: UUID) -> str:
    return f"{SOURCE_PREFIX}{session_id}"


def render_content(claim: ResearchClaim, source: ResearchSource, session_id: UUID) -> str:
    """The note body: the claim plus its provenance (quote, source URL, retrieved date)."""
    title = source.title or source.publisher or ""
    lines = [
        claim.claim_text.strip(),
        "",
        f"引用: {claim.quote.strip()}",
        f"出典: {source.final_url}" + (f" ({title})" if title else ""),
        f"取得日: {source.retrieved_at.date().isoformat()}",
        f"調査ID: {session_id}",
    ]
    return "\n".join(lines)


class ResearchMemoryCandidates:
    def __init__(self, research: ResearchRepository, memory: MemoryRepository) -> None:
        self._research = research
        self._memory = memory

    def _verified(self, session_id: UUID) -> list[tuple[ResearchClaim, ResearchSource]]:
        sources = {source.id: source for source in self._research.list_sources(session_id)}
        in_open_conflict: set[UUID] = set()
        for conflict in self._research.list_conflicts(session_id):
            if conflict.status is ConflictStatus.OPEN:
                in_open_conflict.add(conflict.claim_a_id)
                if conflict.claim_b_id is not None:
                    in_open_conflict.add(conflict.claim_b_id)
        pairs = []
        for claim in self._research.list_claims(session_id):
            source = sources.get(claim.source_id)
            if (
                source is None
                or claim.id in in_open_conflict
                or not claim.claim_text.strip()
                or not claim.quote.strip()
            ):
                continue
            pairs.append((claim, source))
        return pairs

    def _require_completed(self, session_id: UUID) -> None:
        session = self._research.get_session(session_id)
        if session is None:
            raise RefusedCandidates(CandidateRefusal.SESSION_NOT_FOUND)
        if session.status is not ResearchStatus.COMPLETED:
            raise RefusedCandidates(CandidateRefusal.SESSION_NOT_COMPLETED)

    def existing(self, session_id: UUID) -> CandidateSet:
        """Candidates already staged for the session (read-only)."""
        session = self._research.get_session(session_id)
        if session is None:
            raise RefusedCandidates(CandidateRefusal.SESSION_NOT_FOUND)
        if session.status is not ResearchStatus.COMPLETED:
            return CandidateSet((), 0, 0, eligible=False)
        pairs = self._verified(session_id)
        found = []
        for claim, _ in pairs[:MAX_CANDIDATES_PER_SESSION]:
            stored = self._memory.get(candidate_id(claim.id))
            if stored is not None:
                found.append(stored)
        return CandidateSet(
            tuple(found), 0, max(0, len(pairs) - MAX_CANDIDATES_PER_SESSION), bool(pairs)
        )

    def stage(self, session_id: UUID) -> CandidateSet:
        """Stage one pending candidate per verified claim (capped); repeats add nothing."""
        self._require_completed(session_id)
        pairs = self._verified(session_id)
        if not pairs:
            raise RefusedCandidates(CandidateRefusal.NO_VERIFIED_CLAIMS)
        stored_all: list[StoredMemory] = []
        created = 0
        for claim, source in pairs[:MAX_CANDIDATES_PER_SESSION]:
            record = MemoryRecord(
                id=candidate_id(claim.id),
                category=MemoryCategory.PROJECT,
                content=render_content(claim, source, session_id),
                source=research_source(session_id),
                origin=MemoryOrigin.RESEARCH,
                importance=IMPORTANCE,
                confidence=CONFIDENCE,
                tags=(RESEARCH_TAG,),
            )
            try:
                stored_all.append(self._memory.add(record))
                created += 1
            except MemoryAlreadyExists:
                existing = self._memory.get(record.id)
                if existing is None:
                    raise MemoryRepositoryError("Memory storage unavailable") from None
                stored_all.append(existing)
        return CandidateSet(
            tuple(stored_all), created, max(0, len(pairs) - MAX_CANDIDATES_PER_SESSION)
        )
