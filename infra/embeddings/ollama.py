"""Ollama embeddings. Ollama uses the GPU when one is available.

Qwen3-Embedding encodes a query as `Instruct: …\\nQuery:…` and a document as the raw text.
Every vector is L2-normalized. `dimensions` asks Ollama to truncate Matryoshka models to EMBEDDING_DIMENSIONS.
"""

import math
from collections.abc import Sequence
from typing import Any

import httpx

from common.config import Settings
from infra.embeddings.base import EmbeddingConfigError, EmbeddingError, Vector

DEFAULT_MODEL = "qwen3-embedding:0.6b"
_QUERY_TASK = "Given a search query, retrieve relevant passages that answer the query"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def query_text(model: str, text: str) -> str:
    """Retrieval instruction for Qwen3-Embedding. Other models are sent as-is."""
    if "qwen" in model.lower() and "embed" in model.lower():
        return f"Instruct: {_QUERY_TASK}\nQuery:{text}"
    return text


class OllamaEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        model: str = DEFAULT_MODEL,
        dimensions: int = 1024,
        batch_size: int = 32,
        client: Any = None,
    ) -> None:
        if dimensions <= 0:
            raise EmbeddingConfigError("Embedding dimensions must be positive")
        if batch_size < 1:
            raise EmbeddingConfigError("Embedding batch size must be at least 1")
        if not base_url.strip():
            raise EmbeddingConfigError("OLLAMA_BASE_URL is not set")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        # The first batch also waits for Ollama to load the model into GPU memory.
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0),
            )
        )
        self._owns_client = client is None

    @classmethod
    def from_settings(cls, settings: Settings) -> "OllamaEmbedder":
        return cls(
            base_url=settings.ollama_base_url,
            model=settings.embedding_model or DEFAULT_MODEL,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
        )

    @property
    def provider(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._embed(texts[start : start + self._batch_size]))
        return vectors

    async def embed_query(self, text: str) -> Vector:
        if not text.strip():
            raise EmbeddingError("Cannot embed empty text")
        return (await self._embed([query_text(self._model, text)]))[0]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _embed(self, texts: Sequence[str]) -> list[Vector]:
        if any(not text.strip() for text in texts):
            raise EmbeddingError("Cannot embed empty text")
        try:
            response = await self._client.post(
                "/api/embed",
                json={
                    "model": self._model,
                    "input": list(texts),
                    "truncate": True,
                    "dimensions": self._dimensions,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.strip()
            raise EmbeddingError(
                f"Ollama embedding request failed ({exc.response.status_code}): {body}",
                retryable=exc.response.status_code in _RETRYABLE_STATUS,
            ) from exc
        except httpx.TimeoutException as exc:
            raise EmbeddingError(
                f"Ollama timed out embedding {len(texts)} texts at {self._base_url} ({type(exc).__name__})",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise EmbeddingError(f"Ollama is unreachable at {self._base_url}: {detail}", retryable=True) from exc

        embeddings = response.json().get("embeddings") or []
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"Ollama returned {len(embeddings)} embeddings for {len(texts)} texts")
        return [self._normalized(values) for values in embeddings]

    def _normalized(self, values: Sequence[float] | None) -> Vector:
        if not values or len(values) != self._dimensions:
            raise EmbeddingError(f"Ollama returned a vector of size {len(values or [])}, expected {self._dimensions}")
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise EmbeddingError("Ollama returned a zero vector")
        return [value / norm for value in values]
