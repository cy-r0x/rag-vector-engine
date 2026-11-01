import pytest

from common.config import Settings
from infra.embeddings import PROVIDERS, EmbeddingConfigError, create_embedder
from infra.embeddings.gemini import GeminiEmbedder


def test_gemini_is_the_default_provider():
    embedder = create_embedder(Settings(gemini_api_key="key"))
    assert isinstance(embedder, GeminiEmbedder)
    assert (embedder.model, embedder.dimensions) == ("gemini-embedding-001", 768)


def test_provider_name_is_case_insensitive():
    assert isinstance(create_embedder(Settings(embedding_provider=" Gemini ", gemini_api_key="key")), GeminiEmbedder)


def test_unknown_provider_lists_the_known_ones():
    with pytest.raises(EmbeddingConfigError, match="Unknown EMBEDDING_PROVIDER 'acme'.*gemini"):
        create_embedder(Settings(embedding_provider="acme"))


def test_new_providers_plug_in_through_the_registry(monkeypatch: pytest.MonkeyPatch):
    class StubEmbedder:
        provider, model, dimensions = "stub", "stub-1", 3

        async def embed_documents(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

        async def embed_query(self, text):
            return [1.0, 0.0, 0.0]

        async def close(self):
            pass

    monkeypatch.setitem(PROVIDERS, "stub", lambda settings: StubEmbedder())
    assert create_embedder(Settings(embedding_provider="stub")).provider == "stub"
