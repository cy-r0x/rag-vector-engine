"""Calls the real Gemini API; runs only when GEMINI_API_KEY is configured (environment or .env)."""

import math

import pytest

from common.config import Settings
from infra.embeddings import create_embedder

_SETTINGS = Settings()

pytestmark = pytest.mark.skipif(not _SETTINGS.gemini_api_key, reason="GEMINI_API_KEY is not set")


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_live_embeddings_rank_the_relevant_chunk_first():
    embedder = create_embedder(_SETTINGS)
    try:
        documents = await embedder.embed_documents(
            [
                "Quarterly revenue grew 12% year over year, driven by the North region.",
                "The office cafeteria now serves vegetarian lunch on Fridays.",
            ]
        )
        query = await embedder.embed_query("How much did revenue grow?")
    finally:
        await embedder.close()

    assert all(len(vector) == embedder.dimensions for vector in [*documents, query])
    assert all(math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6) for vector in documents)
    assert _cosine(query, documents[0]) > _cosine(query, documents[1])
