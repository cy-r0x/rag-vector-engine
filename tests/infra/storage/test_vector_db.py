import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
from qdrant_client import AsyncQdrantClient

from infra.storage.vector_db import QdrantVectorStore, VectorStoreError, chunk_point_id
from workers.chunker import Chunk

QDRANT_URL = os.getenv("TEST_QDRANT_URL", "http://localhost:6333")


def _qdrant_reachable() -> bool:
    parsed = urlparse(QDRANT_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6333), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _qdrant_reachable(), reason=f"Qdrant not reachable at {QDRANT_URL}")


@pytest.fixture
async def store():
    collection = f"test_chunks_{uuid.uuid4().hex}"
    client = AsyncQdrantClient(url=QDRANT_URL)
    vector_store = QdrantVectorStore(client, collection)
    await vector_store.ensure_collection(3)
    try:
        yield vector_store
    finally:
        await client.delete_collection(collection)
        await client.close()


def _chunks(*texts: str) -> list[Chunk]:
    chunks, offset = [], 0
    for index, text in enumerate(texts):
        chunks.append(Chunk(index=index, text=text, start=offset, end=offset + len(text)))
        offset += len(text) + 2
    return chunks


async def test_search_never_crosses_tenants(store: QdrantVectorStore):
    acme, globex = uuid.uuid7(), uuid.uuid7()
    acme_doc, globex_doc = uuid.uuid7(), uuid.uuid7()
    await store.replace_document_chunks(
        business_id=acme, doc_id=acme_doc, chunks=_chunks("acme revenue"), vectors=[[0.0, 1.0, 0.0]]
    )
    # Globex's vector is an exact match for the query, Acme's is orthogonal.
    await store.replace_document_chunks(
        business_id=globex, doc_id=globex_doc, chunks=_chunks("globex secret"), vectors=[[1.0, 0.0, 0.0]]
    )

    hits = await store.search(business_id=acme, vector=[1.0, 0.0, 0.0], limit=10)

    assert [(hit.doc_id, hit.text) for hit in hits] == [(acme_doc, "acme revenue")]
    assert await store.search(business_id=uuid.uuid7(), vector=[1.0, 0.0, 0.0]) == []


async def test_search_ranks_by_similarity_and_returns_chunk_payload(store: QdrantVectorStore):
    business, doc = uuid.uuid7(), uuid.uuid7()
    chunks = _chunks("about revenue", "about lunch")
    await store.replace_document_chunks(
        business_id=business, doc_id=doc, chunks=chunks, vectors=[[1.0, 0.1, 0.0], [0.0, 1.0, 0.1]]
    )

    hits = await store.search(business_id=business, vector=[1.0, 0.0, 0.0], limit=2)

    assert [hit.chunk_index for hit in hits] == [0, 1]
    assert hits[0].score > hits[1].score
    assert (hits[0].text, hits[0].start, hits[0].end) == ("about revenue", chunks[0].start, chunks[0].end)


async def test_search_can_be_narrowed_to_documents(store: QdrantVectorStore):
    business, doc_a, doc_b = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    await store.replace_document_chunks(business_id=business, doc_id=doc_a, chunks=_chunks("a"), vectors=[[1, 0, 0]])
    await store.replace_document_chunks(business_id=business, doc_id=doc_b, chunks=_chunks("b"), vectors=[[1, 0, 0]])

    hits = await store.search(business_id=business, vector=[1.0, 0.0, 0.0], doc_ids=[doc_b])

    assert [hit.doc_id for hit in hits] == [doc_b]


async def test_reindexing_overwrites_and_drops_stale_chunks(store: QdrantVectorStore):
    business, doc = uuid.uuid7(), uuid.uuid7()
    await store.replace_document_chunks(
        business_id=business, doc_id=doc, chunks=_chunks("old 0", "old 1", "old 2"), vectors=[[1, 0, 0]] * 3
    )
    await store.replace_document_chunks(
        business_id=business, doc_id=doc, chunks=_chunks("new 0"), vectors=[[1, 0, 0]]
    )

    hits = await store.search(business_id=business, vector=[1.0, 0.0, 0.0], limit=10)

    assert [hit.text for hit in hits] == ["new 0"]


async def test_delete_document_only_removes_that_document(store: QdrantVectorStore):
    business, doc_a, doc_b = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    await store.replace_document_chunks(business_id=business, doc_id=doc_a, chunks=_chunks("a"), vectors=[[1, 0, 0]])
    await store.replace_document_chunks(business_id=business, doc_id=doc_b, chunks=_chunks("b"), vectors=[[1, 0, 0]])
    # The same doc_id under another tenant must not be touched either.
    other = uuid.uuid7()
    await store.replace_document_chunks(business_id=other, doc_id=doc_a, chunks=_chunks("x"), vectors=[[1, 0, 0]])

    await store.delete_document(business_id=business, doc_id=doc_a)

    assert [h.doc_id for h in await store.search(business_id=business, vector=[1, 0, 0])] == [doc_b]
    assert [h.text for h in await store.search(business_id=other, vector=[1, 0, 0])] == ["x"]


async def test_ensure_collection_is_idempotent_and_checks_dimensions(store: QdrantVectorStore):
    await store.ensure_collection(3)
    with pytest.raises(VectorStoreError, match="stores 3-dimensional vectors but the embedder produces 768"):
        await store.ensure_collection(768)


async def test_vector_count_must_match_chunks(store: QdrantVectorStore):
    with pytest.raises(ValueError, match="2 vectors for 1 chunks"):
        await store.replace_document_chunks(
            business_id=uuid.uuid7(), doc_id=uuid.uuid7(), chunks=_chunks("a"), vectors=[[1, 0, 0], [0, 1, 0]]
        )


def test_point_ids_are_deterministic_per_document_and_chunk():
    doc = uuid.uuid7()
    assert chunk_point_id(doc, 3) == chunk_point_id(doc, 3)
    assert chunk_point_id(doc, 3) != chunk_point_id(doc, 4)
    assert chunk_point_id(doc, 3) != chunk_point_id(uuid.uuid7(), 3)
