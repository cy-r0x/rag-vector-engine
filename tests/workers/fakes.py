"""In-memory stand-ins for the embedder and vector store used by the worker tests."""

from uuid import UUID

from infra.embeddings import EmbeddingError


class FakeEmbedder:
    provider, model, dimensions = "fake", "fake-embedding", 3

    def __init__(self) -> None:
        self.failures: list[EmbeddingError] = []
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts):
        self.calls.append(list(texts))
        if self.failures:
            raise self.failures.pop(0)
        return [[float(len(text)), 1.0, 0.0] for text in texts]

    async def embed_query(self, text):
        return [float(len(text)), 1.0, 0.0]

    async def close(self):
        pass


class InMemoryVectorStore:
    def __init__(self) -> None:
        self.dimensions: int | None = None
        self.documents: dict[tuple[UUID, UUID], list[tuple[int, str, list[float]]]] = {}

    async def ensure_collection(self, dimensions: int) -> None:
        self.dimensions = dimensions

    async def replace_document_chunks(self, *, business_id, doc_id, chunks, vectors) -> None:
        self.documents[(business_id, doc_id)] = [
            (chunk.index, chunk.text, list(vector)) for chunk, vector in zip(chunks, vectors, strict=True)
        ]

    async def delete_document(self, *, business_id, doc_id) -> None:
        self.documents.pop((business_id, doc_id), None)

    async def search(self, *, business_id, vector, limit=5, doc_ids=None):
        return []

    async def close(self) -> None:
        pass
