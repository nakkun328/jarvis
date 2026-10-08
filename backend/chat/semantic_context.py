"""Explicit semantic chat context with the lexical context's safety bounds."""

import asyncio

from backend.chat.memory_context import _MAX_MATCHES, MemoryContext, MemoryContextError
from backend.memory.retrieval import RetrievalResult
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
        try:
            self.retriever.verify_current_matches(result)
        except ValueError as exc:
            raise MemoryContextError("Memory changed during semantic retrieval") from exc
