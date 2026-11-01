"""Embedding provider registry.

To add a provider: implement `Embedder` in its own module (e.g. `infra/embeddings/openai.py`) and register a
builder below. Builders import their SDK lazily, so only the configured provider's dependencies are needed.
"""

from collections.abc import Callable
from functools import lru_cache

from common.config import Settings, get_settings
from infra.embeddings.base import Embedder, EmbeddingConfigError


def _gemini(settings: Settings) -> Embedder:
    from infra.embeddings.gemini import GeminiEmbedder

    return GeminiEmbedder.from_settings(settings)


PROVIDERS: dict[str, Callable[[Settings], Embedder]] = {
    "gemini": _gemini,
}


def create_embedder(settings: Settings) -> Embedder:
    """Build the embedder selected by EMBEDDING_PROVIDER."""
    name = settings.embedding_provider.strip().lower()
    builder = PROVIDERS.get(name)
    if builder is None:
        known = ", ".join(sorted(PROVIDERS))
        raise EmbeddingConfigError(f"Unknown EMBEDDING_PROVIDER {settings.embedding_provider!r}; expected one of: {known}")
    return builder(settings)


@lru_cache
def get_embedder() -> Embedder:
    return create_embedder(get_settings())
