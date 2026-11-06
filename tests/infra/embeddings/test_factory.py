import pytest

from common.config import Settings
from infra.embeddings import PROVIDERS, EmbeddingConfigError, create_embedder
from infra.embeddings.ollama import OllamaEmbedder


def test_ollama_is_the_default_provider():
    assert Settings.model_fields["embedding_provider"].default == "ollama"

    embedder = create_embedder(
        Settings(
            embedding_model="",
            embedding_dimensions=1024,
            embedding_batch_size=32,
            ollama_base_url="http://127.0.0.1:11434",
        )
    )
    assert embedder.provider == "ollama"
    assert (embedder.model, embedder.dimensions) == ("qwen3-embedding:0.6b", 1024)


def test_provider_name_is_case_insensitive():
    embedder = create_embedder(Settings(embedding_provider=" Ollama ", ollama_base_url="http://127.0.0.1:11434"))
    assert isinstance(embedder, OllamaEmbedder)


def test_unknown_provider_lists_the_known_ones():
    with pytest.raises(EmbeddingConfigError, match="Unknown EMBEDDING_PROVIDER 'acme'.*ollama"):
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
