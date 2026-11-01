"""Tenant-isolated vector storage in Qdrant.

All tenants share one collection. Every point carries a `business_id` payload, indexed as the tenant key
(`is_tenant`), and every read or delete takes a mandatory `business_id` that is applied as a filter inside the
search itself (Qdrant's filterable HNSW), never as a post-filter. The global HNSW graph is disabled (`m=0`) in
favour of per-tenant graphs (`payload_m`), so a query without a tenant filter isn't even efficient.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models

from common.config import get_settings

_UPSERT_BATCH_SIZE = 256
_TENANT_HNSW_M = 16


class VectorStoreError(Exception):
    pass


class Chunk(Protocol):
    """A span of a document's extracted Markdown (e.g. `workers.chunker.Chunk`)."""

    @property
    def index(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def start(self) -> int: ...

    @property
    def end(self) -> int: ...


@dataclass(frozen=True)
class ChunkHit:
    doc_id: UUID
    chunk_index: int
    text: str
    start: int
    end: int
    score: float


class VectorStore(Protocol):
    async def ensure_collection(self, dimensions: int) -> None: ...

    async def replace_document_chunks(
        self, *, business_id: UUID, doc_id: UUID, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> None:
        """Make `chunks` the document's only points (stale chunks from an earlier indexing are removed)."""
        ...

    async def delete_document(self, *, business_id: UUID, doc_id: UUID) -> None: ...

    async def search(
        self,
        *,
        business_id: UUID,
        vector: Sequence[float],
        limit: int = 5,
        min_score: float | None = None,
        doc_ids: Sequence[UUID] | None = None,
    ) -> list[ChunkHit]:
        """The tenant's most similar chunks, best first; with `min_score`, only chunks scoring at least that."""
        ...

    async def close(self) -> None: ...


def chunk_point_id(doc_id: UUID, chunk_index: int) -> UUID:
    """Deterministic point id, so re-indexing a document overwrites its points instead of duplicating them."""
    return uuid.uuid5(doc_id, f"chunk:{chunk_index}")


class QdrantVectorStore:
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self._client = client
        self._collection = collection

    @classmethod
    def from_url(cls, url: str, collection: str, api_key: str | None = None) -> "QdrantVectorStore":
        return cls(AsyncQdrantClient(url=url, api_key=api_key), collection)

    async def ensure_collection(self, dimensions: int) -> None:
        """Create the collection and its payload indexes if missing; fail if it exists with another vector size."""
        if await self._client.collection_exists(self._collection):
            info = await self._client.get_collection(self._collection)
            vectors = info.config.params.vectors
            size = vectors.size if isinstance(vectors, models.VectorParams) else None
            if size != dimensions:
                raise VectorStoreError(
                    f"Qdrant collection {self._collection!r} stores {size}-dimensional vectors but the embedder "
                    f"produces {dimensions}; use a new QDRANT_COLLECTION or re-create it and re-index"
                )
        else:
            await self._client.create_collection(
                self._collection,
                vectors_config=models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
                hnsw_config=models.HnswConfigDiff(payload_m=_TENANT_HNSW_M, m=0),
            )
        await self._client.create_payload_index(
            self._collection,
            "business_id",
            field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
        )
        await self._client.create_payload_index(
            self._collection, "doc_id", field_schema=models.PayloadSchemaType.KEYWORD
        )
        await self._client.create_payload_index(
            self._collection, "chunk_index", field_schema=models.PayloadSchemaType.INTEGER
        )

    async def replace_document_chunks(
        self, *, business_id: UUID, doc_id: UUID, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"Got {len(vectors)} vectors for {len(chunks)} chunks")
        points = [
            models.PointStruct(
                id=str(chunk_point_id(doc_id, chunk.index)),
                vector=list(vector),
                payload={
                    "business_id": str(business_id),
                    "doc_id": str(doc_id),
                    "chunk_index": chunk.index,
                    "text": chunk.text,
                    "start": chunk.start,
                    "end": chunk.end,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(points), _UPSERT_BATCH_SIZE):
            await self._client.upsert(self._collection, points=points[start : start + _UPSERT_BATCH_SIZE], wait=True)

        # Point ids are deterministic, so the upsert overwrote existing chunks in place; only chunks beyond the
        # new count are stale. Deleting them afterwards keeps the document searchable throughout.
        stale = _tenant_filter(business_id, [doc_id])
        stale.must.append(models.FieldCondition(key="chunk_index", range=models.Range(gte=len(chunks))))
        await self._client.delete(self._collection, points_selector=models.FilterSelector(filter=stale), wait=True)

    async def delete_document(self, *, business_id: UUID, doc_id: UUID) -> None:
        await self._client.delete(
            self._collection,
            points_selector=models.FilterSelector(filter=_tenant_filter(business_id, [doc_id])),
            wait=True,
        )

    async def search(
        self,
        *,
        business_id: UUID,
        vector: Sequence[float],
        limit: int = 5,
        min_score: float | None = None,
        doc_ids: Sequence[UUID] | None = None,
    ) -> list[ChunkHit]:
        response = await self._client.query_points(
            self._collection,
            query=list(vector),
            query_filter=_tenant_filter(business_id, doc_ids),
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
        )
        return [
            ChunkHit(
                doc_id=UUID(point.payload["doc_id"]),
                chunk_index=point.payload["chunk_index"],
                text=point.payload["text"],
                start=point.payload["start"],
                end=point.payload["end"],
                score=point.score,
            )
            for point in response.points
        ]

    async def close(self) -> None:
        await self._client.close()


def _tenant_filter(business_id: UUID, doc_ids: Sequence[UUID] | None = None) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="business_id", match=models.MatchValue(value=str(business_id)))
    ]
    if doc_ids is not None:
        must.append(models.FieldCondition(key="doc_id", match=models.MatchAny(any=[str(d) for d in doc_ids])))
    return models.Filter(must=must)


@lru_cache
def get_vector_store() -> VectorStore:
    settings = get_settings()
    return QdrantVectorStore.from_url(settings.qdrant_url, settings.qdrant_collection, settings.qdrant_api_key)
