from collections.abc import Sequence
from typing import Protocol

Vector = list[float]


class EmbeddingError(Exception):
    """The provider could not embed the input.

    `retryable` is True for transient failures (network, rate limit, provider outage) where trying again later
    can succeed, and False when the input itself was rejected or the response was unusable.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmbeddingConfigError(EmbeddingError):
    """The embedder is misconfigured (unknown provider, missing API key, ...)."""


class Embedder(Protocol):
    """A text embedding provider. Callers depend only on this interface, never on a provider SDK.

    Documents and queries are embedded separately because retrieval-tuned models encode them differently.
    Vectors are L2-normalized, so dot product equals cosine similarity.
    """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        """One vector per text, in input order."""
        ...

    async def embed_query(self, text: str) -> Vector: ...

    async def close(self) -> None: ...
