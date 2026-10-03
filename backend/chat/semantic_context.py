"""Explicit semantic chat context with the lexical context's safety bounds."""

import asyncio

from backend.chat.memory_context import _MAX_MATCHES, MemoryContext, MemoryContextError
from backend.memory.repository import MemoryStatus
from backend.memory.retrieval import RetrievalResult, RetrievedMemory
from backend.memory.semantic import SemanticMemorySearcher


class SemanticMemoryContext(MemoryContext):
    """Resolve semantic candidate IDs to current approved canonical notes.

    Retrieval failure is never a silent switch to another search strategy.
    Cancellation propagates instead of being turned into a successful turn.
    """

    def __init__(self, searcher: SemanticMemorySearcher) -> None:
        super().__init__(searcher.retriever)
        self.searcher = searcher

    async def for_query(self, query: str) -> str | None:
        try:
            result = await self.searcher.search(query, limit=_MAX_MATCHES)
            await asyncio.to_thread(self._verify_result, result)
            return self._render_result(result)
        except Exception as exc:
            # Concrete embedding/index SDK exceptions may contain credentials,
            # note content or paths. The HTTP/SSE boundary logs only this type.
            raise MemoryContextError("Semantic memory retrieval unavailable") from exc

    def _verify_result(self, result: RetrievalResult) -> None:
        for match in result.matches:
            current = self.retriever.get_approved(match.record.id)
            # get_approved reads SQLite before resolving the vault. A review
            # or edit can interleave with that resolution, so do not accept
            # its earlier status/revision as the final check.
            note = self.retriever.vault.read(match.record.id)
            latest = self.retriever.repository.get(match.record.id)
            if (
                not isinstance(current, RetrievedMemory)
                or current.note_revision != match.note_revision
                or current.record != match.record
                or note is None
                or note.revision != match.note_revision
                or latest is None
                or latest.status is not MemoryStatus.APPROVED
            ):
                raise MemoryContextError("Memory changed during semantic retrieval")
