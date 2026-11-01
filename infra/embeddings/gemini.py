import math
from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import errors, types

from common.config import Settings
from infra.embeddings.base import EmbeddingConfigError, EmbeddingError, Vector

DEFAULT_MODEL = "gemini-embedding-001"
MAX_BATCH_SIZE = 100

_TIMEOUT_MS = 60_000
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_RETRY = types.HttpRetryOptions(
    attempts=4, initial_delay=1.0, max_delay=30.0, http_status_codes=sorted(_RETRYABLE_STATUS_CODES)
)


class GeminiEmbedder:
    """Gemini text embeddings (Google GenAI SDK), with retrieval task types and a reduced output dimensionality.

    Only Gemini's full-size output is pre-normalized, so every vector is L2-normalized here.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        dimensions: int = 768,
        batch_size: int = MAX_BATCH_SIZE,
        client: Any = None,
    ) -> None:
        if dimensions <= 0:
            raise EmbeddingConfigError("Embedding dimensions must be positive")
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise EmbeddingConfigError(f"Gemini embedding batch size must be between 1 and {MAX_BATCH_SIZE}")
        if client is None:
            if not api_key:
                raise EmbeddingConfigError("GEMINI_API_KEY is not set")
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=_TIMEOUT_MS, retry_options=_RETRY))
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size

    @classmethod
    def from_settings(cls, settings: Settings) -> "GeminiEmbedder":
        return cls(
            api_key=settings.gemini_api_key,
            model=settings.embedding_model or DEFAULT_MODEL,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
        )

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._embed(texts[start : start + self._batch_size], "RETRIEVAL_DOCUMENT"))
        return vectors

    async def embed_query(self, text: str) -> Vector:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]

    async def close(self) -> None:
        await self._client.aio.aclose()

    async def _embed(self, texts: Sequence[str], task_type: str) -> list[Vector]:
        if any(not text.strip() for text in texts):
            raise EmbeddingError("Cannot embed empty text")
        try:
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=list(texts),
                config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=self._dimensions),
            )
        except errors.APIError as exc:
            raise EmbeddingError(
                f"Gemini embedding request failed ({exc.code} {exc.status}): {exc.message}",
                retryable=exc.code in _RETRYABLE_STATUS_CODES,
            ) from exc
        except Exception as exc:
            raise EmbeddingError(f"Gemini embedding request failed: {exc}", retryable=True) from exc

        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"Gemini returned {len(embeddings)} embeddings for {len(texts)} texts")
        return [self._normalized(embedding.values) for embedding in embeddings]

    def _normalized(self, values: Sequence[float] | None) -> Vector:
        if not values or len(values) != self._dimensions:
            raise EmbeddingError(f"Gemini returned a vector of size {len(values or [])}, expected {self._dimensions}")
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise EmbeddingError("Gemini returned a zero vector")
        return [value / norm for value in values]
