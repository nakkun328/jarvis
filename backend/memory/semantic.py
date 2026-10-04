"""Encode a query in the indexed space, then resolve canonical memories."""

from backend.memory.embedding import EmbeddingProvider, EmbeddingSpace, embed_texts
from backend.memory.retrieval import MemoryRetriever, RetrievalResult

_MAX_QUERY_CHARS = 4000


class SemanticMemorySearcher:
    """Optional semantic retrieval; the index supplies IDs, never facts."""

    def __init__(self, retriever: MemoryRetriever, provider: EmbeddingProvider) -> None:
        if retriever.vector_index is None:
            raise ValueError("Semantic search requires a vector index")
        if not isinstance(provider.space, EmbeddingSpace):
            raise ValueError("Provider must declare an embedding space")
        self.retriever = retriever
        self.provider = provider

    async def search(self, query: str, *, limit: int = 10) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise ValueError("query must contain 1 to 4000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        space = self.provider.space
        if not isinstance(space, EmbeddingSpace):
            raise ValueError("Provider must declare an embedding space")
        vector = (await embed_texts(self.provider, (query,)))[0]
        if self.provider.space != space:
            raise ValueError("Embedding contract changed during semantic search")
        result = await self.retriever.search_vector(space.query(vector, limit=limit))
        if self.provider.space != space:
            raise ValueError("Embedding contract changed during semantic search")
        return result
