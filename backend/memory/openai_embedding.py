"""Explicit, optional OpenAI implementation of the embedding contract."""

from collections.abc import Sequence

from openai import APIError, AsyncOpenAI

from backend.memory.embedding import EmbeddingSpace


class OpenAIEmbeddingError(RuntimeError):
    """The remote embedding request or its response could not be used safely."""


class OpenAIEmbeddingProvider:
    """Generate vectors in one caller-selected, versioned embedding space.

    The caller owns the client and opts in to sending text to OpenAI. No model,
    credential, or data-sharing policy is chosen by this adapter.
    """

    def __init__(self, space: EmbeddingSpace, client: AsyncOpenAI) -> None:
        if not isinstance(space, EmbeddingSpace):
            raise ValueError("space must be an EmbeddingSpace")
        self.space = space
        self._client = client

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if isinstance(texts, (str, bytes)):
            raise ValueError("texts must contain nonempty strings")
        try:
            batch = tuple(texts)
        except TypeError as exc:
            raise ValueError("texts must contain nonempty strings") from exc
        if any(not isinstance(text, str) or not text.strip() for text in batch):
            raise ValueError("texts must contain nonempty strings")
        if not batch:
            return ()

        try:
            response = await self._client.embeddings.create(
                model=self.space.name,
                input=list(batch),
                dimensions=self.space.dimension,
                encoding_format="float",
            )
        except APIError:
            raise OpenAIEmbeddingError("OpenAI embedding request failed") from None

        try:
            if response.model != self.space.name or len(response.data) != len(batch):
                raise ValueError("Embedding model or result count differs")
            ordered: list[tuple[float, ...] | None] = [None] * len(batch)
            for item in response.data:
                index = item.index
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(batch)
                    or ordered[index] is not None
                ):
                    raise ValueError("Embedding response indexes are invalid")
                ordered[index] = self.space.validate(item.embedding)
            if any(values is None for values in ordered):
                raise ValueError("Embedding response is incomplete")
            return tuple(values for values in ordered if values is not None)
        except (AttributeError, TypeError, ValueError):
            raise OpenAIEmbeddingError("OpenAI embedding response is invalid") from None
