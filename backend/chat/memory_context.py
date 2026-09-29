"""Bounded, lower-trust context from reviewed long-term memory."""

import asyncio
import json

from backend.memory.repository import MemoryRepositoryError
from backend.memory.retrieval import MemoryRetriever

_MAX_MATCHES = 3
_MAX_CONTENT = 500
_MAX_SOURCE = 200
_MAX_CONTEXT = 2400


class MemoryContextError(RuntimeError):
    """Configured memory could not be checked safely for a chat request."""


class MemoryContext:
    def __init__(self, retriever: MemoryRetriever) -> None:
        self.retriever = retriever

    async def for_query(self, query: str) -> str | None:
        try:
            result = await asyncio.to_thread(
                self.retriever.search_text, query, limit=_MAX_MATCHES
            )
        except (MemoryRepositoryError, OSError) as exc:
            raise MemoryContextError("Memory retrieval unavailable") from exc
        if result.issues:
            raise MemoryContextError("An approved memory note could not be verified")
        if not result.matches:
            return None

        items: list[dict[str, object]] = []
        for match in result.matches:
            record = match.record
            item: dict[str, object] = {
                "id": str(record.id),
                "category": record.category.value,
                "source": record.source[:_MAX_SOURCE],
                "source_truncated": len(record.source) > _MAX_SOURCE,
                "origin": record.origin.value,
                "content": record.content[:_MAX_CONTENT],
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
