import httpx
import pytest

from common.config import Settings
from infra.embeddings import EmbeddingConfigError, EmbeddingError
from infra.embeddings.ollama import DEFAULT_MODEL, OllamaEmbedder, query_text


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)
        self.request = httpx.Request("POST", "http://ollama/api/embed")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=self.request, response=httpx.Response(self.status_code, text=self.text, request=self.request))

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []
        self.closed = False

    async def post(self, url: str, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "json": json})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def _embedder(client: FakeClient, *, model: str = DEFAULT_MODEL, dimensions: int = 2, batch_size: int = 2) -> OllamaEmbedder:
    return OllamaEmbedder(
        base_url="http://127.0.0.1:11434", model=model, dimensions=dimensions, batch_size=batch_size, client=client
    )


async def test_documents_are_batched_and_queries_get_the_qwen_instruction():
    client = FakeClient(FakeResponse({"embeddings": [[3.0, 0.0], [0.0, 4.0]]}))
    embedder = _embedder(client)

    vectors = await embedder.embed_documents(["alpha", "beta"])
    client.response = FakeResponse({"embeddings": [[5.0, 0.0]]})
    query = await embedder.embed_query("fees")

    assert client.calls[0]["json"]["input"] == ["alpha", "beta"]
    assert client.calls[0]["json"]["dimensions"] == 2
    assert client.calls[1]["json"]["input"] == ["Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery:fees"]
    assert [round(vector[0], 5) for vector in vectors] == [1.0, 0.0]
    assert abs(sum(value * value for value in query) - 1) < 1e-6


def test_query_prefix_is_only_for_qwen_embedding_models():
    assert query_text("qwen3-embedding:0.6b", "fees").startswith("Instruct:")
    assert query_text("nomic-embed-text", "fees") == "fees"


async def test_empty_text_is_rejected():
    client = FakeClient()
    embedder = _embedder(client)

    with pytest.raises(EmbeddingError, match="empty text"):
        await embedder.embed_query("  ")

    assert client.calls == []


async def test_read_timeout_is_reported_as_a_timeout():
    embedder = _embedder(FakeClient(error=httpx.ReadTimeout("")))

    with pytest.raises(EmbeddingError, match="timed out") as timeout_error:
        await embedder.embed_documents(["alpha"])

    assert timeout_error.value.retryable


async def test_connection_errors_are_retryable_and_missing_models_are_not():
    down = _embedder(FakeClient(error=httpx.ConnectError("refused")))
    with pytest.raises(EmbeddingError, match="unreachable") as connect_error:
        await down.embed_documents(["alpha"])
    assert connect_error.value.retryable

    missing = _embedder(FakeClient(FakeResponse({}, status_code=404, text="model not found")))
    with pytest.raises(EmbeddingError, match="404") as missing_error:
        await missing.embed_documents(["alpha"])
    assert not missing_error.value.retryable


def test_from_settings_uses_the_qwen_default():
    embedder = OllamaEmbedder.from_settings(
        Settings(embedding_model="", embedding_dimensions=1024, embedding_batch_size=8, ollama_base_url="http://127.0.0.1:11434")
    )
    assert (embedder.provider, embedder.model, embedder.dimensions) == ("ollama", DEFAULT_MODEL, 1024)


def test_missing_base_url_is_a_config_error():
    with pytest.raises(EmbeddingConfigError, match="OLLAMA_BASE_URL"):
        OllamaEmbedder(base_url="  ")
