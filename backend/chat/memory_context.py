"""Bounded, lower-trust context from reviewed long-term memory."""

import asyncio
import json

from backend.memory.auto_approval import (
    CONTEXT_LABEL_AUTO,
    CONTEXT_LABEL_RESEARCH,
    is_auto_approved,
)
from backend.memory.model import MemoryOrigin, MemoryRecord
from backend.memory.repository import MemoryRepositoryError, MemoryStatus
from backend.memory.retrieval import MemoryRetriever, RetrievalResult

_MAX_MATCHES = 3
_MAX_CONTENT = 500
_MAX_SOURCE = 200
_MAX_CONTEXT = 2400


def rendered_note_count(rendered: str | None) -> int:
    """How many notes a rendered memory reference holds (0 when nothing matched).

    Activity events carry only this number, never the notes themselves.
    """
    if not rendered:
        return 0
    try:
        items = json.loads(rendered)
    except ValueError:
        return 0
    return len(items) if isinstance(items, list) else 0


class MemoryContextError(RuntimeError):
    """Configured memory could not be checked safely for a chat request."""


class MemoryContext:
    def __init__(self, retriever: MemoryRetriever) -> None:
        self.retriever = retriever

    async def for_query(self, query: str) -> str | None:
        try:
            result = await asyncio.to_thread(self._verified_search, query)
        except (MemoryRepositoryError, OSError, TypeError, ValueError) as exc:
            raise MemoryContextError("Memory retrieval unavailable") from exc
        return self._render_result(result)

    def _research_label(self, record: MemoryRecord) -> str:
        """A fixed label in front of research-derived text, so a model reads it as web-derived."""
        if record.origin is not MemoryOrigin.RESEARCH:
            return ""
        try:
            auto = is_auto_approved(self.retriever.repository, record.id)
        except (MemoryRepositoryError, ValueError) as exc:
            raise MemoryContextError("Memory approval history unavailable") from exc
        return (CONTEXT_LABEL_AUTO if auto else CONTEXT_LABEL_RESEARCH) + " "

    def _render_result(self, result: RetrievalResult) -> str | None:
        if result.issues:
            raise MemoryContextError("An approved memory note could not be verified")
        if not result.matches:
            return None

        items: list[dict[str, object]] = []
        for match in result.matches[:_MAX_MATCHES]:
            record = match.record
            label = self._research_label(record)
            item: dict[str, object] = {
                "id": str(record.id),
                "category": record.category.value,
                "source": record.source[:_MAX_SOURCE],
                "source_truncated": len(record.source) > _MAX_SOURCE,
                "origin": record.origin.value,
                "importance": record.importance,
                "confidence": record.confidence,
                "content": label + record.content[:_MAX_CONTENT],
                "content_truncated": len(record.content) > _MAX_CONTENT,
                "stale": match.stale,
                "edited_since_approval": match.edited_since_approval,
            }
            candidate = json.dumps([*items, item], ensure_ascii=False, separators=(",", ":"))
            if len(candidate) > _MAX_CONTEXT:
                break
            items.append(item)
        if not items:
            raise MemoryContextError("Retrieved memory exceeds the context limit")
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))

    def _verified_search(self, query: str) -> RetrievalResult:
        result = self.retriever.search_text(query, limit=_MAX_MATCHES)
        # A review can change while the vault is being scanned. Check the
        # canonical state again immediately before the context is assembled.
        for match in result.matches:
            current = self.retriever.repository.get(match.record.id)
            if current is None or current.status is not MemoryStatus.APPROVED:
                raise MemoryContextError("Memory review state changed during retrieval")
        return result
