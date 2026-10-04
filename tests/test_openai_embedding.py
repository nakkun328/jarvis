"""The optional remote embedding adapter enforces the versioned space contract."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APIError

from backend.memory.embedding import EmbeddingSpace, embed_texts
from backend.memory.openai_embedding import OpenAIEmbeddingError, OpenAIEmbeddingProvider


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = SimpleNamespace(
            model="text-embedding-3-small",
            data=[
                SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                SimpleNamespace(index=0, embedding=[1.0, 0.0]),
            ],
        )
        self.error: Exception | None = None

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _provider(embeddings: FakeEmbeddings) -> OpenAIEmbeddingProvider:
    space = EmbeddingSpace("text-embedding-3-small", "operator-v1", 2)
    return OpenAIEmbeddingProvider(space, SimpleNamespace(embeddings=embeddings))


def test_batch_request_is_explicit_and_results_follow_input_indexes() -> None:
    embeddings = FakeEmbeddings()
    provider = _provider(embeddings)
    vectors = asyncio.run(embed_texts(provider, ["first note", "second note"]))
    assert vectors == ((1.0, 0.0), (0.0, 1.0))
    assert embeddings.calls == [
        {
            "model": "text-embedding-3-small",
            "input": ["first note", "second note"],
            "dimensions": 2,
            "encoding_format": "float",
        }
    ]
    assert provider.space.identifier == "text-embedding-3-small@operator-v1:d2"


def test_empty_input_does_not_make_remote_request() -> None:
    embeddings = FakeEmbeddings()
    assert asyncio.run(_provider(embeddings).embed([])) == ()
    assert embeddings.calls == []


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(model="different-model", data=[SimpleNamespace(index=0, embedding=[1, 0])]),
        SimpleNamespace(model="text-embedding-3-small", data=[]),
        SimpleNamespace(
            model="text-embedding-3-small",
            data=[
                SimpleNamespace(index=0, embedding=[1, 0]),
                SimpleNamespace(index=0, embedding=[0, 1]),
            ],
        ),
        SimpleNamespace(
            model="text-embedding-3-small",
            data=[
                SimpleNamespace(index=0, embedding=[float("nan"), 0]),
                SimpleNamespace(index=1, embedding=[0, 1]),
            ],
        ),
    ],
)
def test_invalid_response_cannot_enter_vector_index(response) -> None:
    embeddings = FakeEmbeddings()
    embeddings.response = response
    with pytest.raises(OpenAIEmbeddingError, match="response is invalid"):
        asyncio.run(_provider(embeddings).embed(["first", "second"]))


def test_api_errors_do_not_expose_remote_error_detail() -> None:
    embeddings = FakeEmbeddings()
    embeddings.error = APIError(
        "private provider detail",
        httpx.Request("POST", "https://api.openai.com/v1/embeddings"),
        body=None,
    )
    with pytest.raises(OpenAIEmbeddingError) as failure:
        asyncio.run(_provider(embeddings).embed(["private note"]))
    assert "private" not in str(failure.value)
    assert failure.value.__cause__ is None
