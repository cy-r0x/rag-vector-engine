import math
from types import SimpleNamespace

import pytest
from google.genai import errors, types

from common.config import Settings
from infra.embeddings import EmbeddingConfigError, EmbeddingError
from infra.embeddings.gemini import DEFAULT_MODEL, GeminiEmbedder


class FakeModels:
    """Stands in for `client.aio.models`; returns one deterministic vector per input text."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[dict] = []
        self.response_override = None
        self.error: Exception | None = None

    async def embed_content(self, *, model, contents, config):
        self.calls.append(
            {
                "model": model,
                "contents": list(contents),
                "task_type": config.task_type,
                "dimensions": config.output_dimensionality,
            }
        )
        if self.error is not None:
            raise self.error
        if self.response_override is not None:
            return self.response_override
        return types.EmbedContentResponse(
            embeddings=[
                types.ContentEmbedding(values=[float(len(text))] + [1.0] * (self.dimensions - 1)) for text in contents
            ]
        )


class FakeClient:
    def __init__(self, dimensions: int) -> None:
        self.closed = False

        async def aclose() -> None:
            self.closed = True

        self.models = FakeModels(dimensions)
        self.aio = SimpleNamespace(models=self.models, aclose=aclose)


def _embedder(*, dimensions: int = 4, batch_size: int = 100, model: str = DEFAULT_MODEL):
    client = FakeClient(dimensions)
    return GeminiEmbedder(api_key=None, model=model, dimensions=dimensions, batch_size=batch_size, client=client), client


async def test_documents_are_batched_in_order_with_the_document_task_type():
    embedder, client = _embedder(batch_size=2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]

    vectors = await embedder.embed_documents(texts)

    assert [call["contents"] for call in client.models.calls] == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    assert {call["task_type"] for call in client.models.calls} == {"RETRIEVAL_DOCUMENT"}
    assert {(call["model"], call["dimensions"]) for call in client.models.calls} == {(DEFAULT_MODEL, 4)}
    # The first component encodes the text length, so order is preserved across batches.
    assert [round(v[0] / v[1]) for v in vectors] == [1, 2, 3, 4, 5]


async def test_query_uses_the_query_task_type():
    embedder, client = _embedder()

    vector = await embedder.embed_query("what was q1 revenue?")

    assert len(vector) == 4
    assert client.models.calls[0]["task_type"] == "RETRIEVAL_QUERY"
    assert client.models.calls[0]["contents"] == ["what was q1 revenue?"]


async def test_vectors_are_l2_normalized():
    embedder, _ = _embedder()
    for vector in await embedder.embed_documents(["some text", "x"]):
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


async def test_no_texts_makes_no_request():
    embedder, client = _embedder()
    assert await embedder.embed_documents([]) == []
    assert client.models.calls == []


async def test_empty_text_is_rejected_before_calling_the_api():
    embedder, client = _embedder()
    with pytest.raises(EmbeddingError, match="empty text"):
        await embedder.embed_documents(["ok", "  "])
    assert client.models.calls == []


@pytest.mark.parametrize(
    ("error", "status", "retryable"),
    [
        (errors.ClientError, 429, True),
        (errors.ServerError, 503, True),
        (errors.ClientError, 400, False),
        (errors.ClientError, 403, False),
    ],
)
async def test_api_errors_are_wrapped_and_classified(error, status: int, retryable: bool):
    embedder, client = _embedder()
    client.models.error = error(status, {"error": {"code": status, "message": "Boom", "status": "SOME_STATUS"}})
    with pytest.raises(EmbeddingError, match=f"{status} SOME_STATUS.*Boom") as raised:
        await embedder.embed_query("hello")
    assert raised.value.retryable is retryable


async def test_network_errors_are_wrapped_as_retryable():
    embedder, client = _embedder()
    client.models.error = ConnectionError("connection reset")
    with pytest.raises(EmbeddingError, match="connection reset") as raised:
        await embedder.embed_query("hello")
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    ("embeddings", "message"),
    [
        ([], "returned 0 embeddings for 1 texts"),
        ([types.ContentEmbedding(values=[1.0, 2.0])], "vector of size 2, expected 4"),
        ([types.ContentEmbedding(values=[0.0, 0.0, 0.0, 0.0])], "zero vector"),
    ],
)
async def test_malformed_responses_are_rejected(embeddings, message):
    embedder, client = _embedder()
    client.models.response_override = types.EmbedContentResponse(embeddings=embeddings)
    with pytest.raises(EmbeddingError, match=message):
        await embedder.embed_query("hello")


async def test_close_closes_the_async_client():
    embedder, client = _embedder()
    await embedder.close()
    assert client.closed


def test_missing_api_key_is_a_config_error():
    with pytest.raises(EmbeddingConfigError, match="GEMINI_API_KEY is not set"):
        GeminiEmbedder(api_key=None)


@pytest.mark.parametrize("batch_size", [0, 101])
def test_batch_size_must_fit_the_gemini_limit(batch_size: int):
    with pytest.raises(EmbeddingConfigError, match="batch size"):
        GeminiEmbedder(api_key="key", batch_size=batch_size)


def test_from_settings_uses_provider_default_model():
    embedder = GeminiEmbedder.from_settings(
        Settings(gemini_api_key="key", embedding_model="", embedding_dimensions=1536, embedding_batch_size=50)
    )
    assert (embedder.provider, embedder.model, embedder.dimensions) == ("gemini", DEFAULT_MODEL, 1536)
