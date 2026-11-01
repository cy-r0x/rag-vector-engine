from collections.abc import Sequence
from uuid import UUID

from infra.embeddings import Embedder
from infra.storage.vector_db import Chunk, VectorStore


async def index_chunks(
    *, embedder: Embedder, store: VectorStore, business_id: UUID, doc_id: UUID, chunks: Sequence[Chunk]
) -> None:
    """Embed a document's chunks and make them the document's vectors in the tenant-scoped store."""
    vectors = await embedder.embed_documents([chunk.text for chunk in chunks])
    await store.replace_document_chunks(business_id=business_id, doc_id=doc_id, chunks=chunks, vectors=vectors)
