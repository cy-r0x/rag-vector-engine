import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from pydantic import ValidationError

from common.config import get_settings
from common.logger import setup_logging
from db.models import DocumentStatus, DocumentStatusEvent, IngestionJob
from infra.embeddings import Embedder, EmbeddingError, get_embedder
from infra.queue import connect, declare_queue, open_channel
from infra.storage.blob import (
    BlobStorage,
    ObjectNotFoundError,
    chunks_key,
    document_prefix,
    extracted_markdown_key,
    get_blob_storage,
)
from infra.storage.vector_db import VectorStore, get_vector_store
from workers.chunker import chunk_markdown, to_jsonl
from workers.embedder import index_chunks
from workers.extractor import ExtractionError, extract_markdown
from workers.publisher import publish_status

logger = logging.getLogger("workers.consumer")

_MAX_LOGGED_BODY_CHARS = 2000
_RETRY_DELAY_SECONDS = 5


@dataclass(frozen=True)
class Pipeline:
    """The services one ingestion job uses."""

    storage: BlobStorage
    embedder: Embedder
    vector_store: VectorStore


class _JobFailed(Exception):
    """The document itself can't be processed; reported as FAILED with this message as the detail."""


async def handle_message(
    message: AbstractIncomingMessage, *, channel: AbstractChannel, events_queue: str, pipeline: Pipeline
) -> None:
    """Extract one document to Markdown, chunk it, embed and index the chunks, and report its status.

    A malformed job is rejected without requeue. Problems with the document (no text layer, corrupt file, missing
    source, input rejected by the embedding provider) are reported as FAILED and acked. Infrastructure errors
    (storage, RabbitMQ, Qdrant, transient embedding errors) requeue the job, so it is only acked once its outcome
    has been recorded on the events queue.
    """
    body = message.body.decode("utf-8", errors="replace")
    logger.info(
        "Received message id=%s routing_key=%s size=%d body=%s",
        message.message_id,
        message.routing_key,
        len(message.body),
        body[:_MAX_LOGGED_BODY_CHARS],
    )

    try:
        job = IngestionJob.model_validate_json(message.body)
    except ValidationError as exc:
        logger.error("Rejecting malformed ingestion job id=%s: %s", message.message_id, exc)
        await message.reject(requeue=False)
        return

    report = partial(_report, channel, events_queue, job)
    try:
        await report(DocumentStatus.PROCESSING)
        try:
            await _process(job, pipeline)
        except _JobFailed as exc:
            logger.warning("Failed doc_id=%s: %s", job.doc_id, exc)
            await report(DocumentStatus.FAILED, detail=str(exc))
        else:
            await report(DocumentStatus.INDEXED)
    except Exception:
        logger.exception("Infrastructure error while processing doc_id=%s; requeueing the job", job.doc_id)
        await asyncio.sleep(_RETRY_DELAY_SECONDS)
        await message.nack(requeue=True)
        return

    await message.ack()


async def _process(job: IngestionJob, pipeline: Pipeline) -> None:
    storage = pipeline.storage
    business_id, doc_id = str(job.business_id), str(job.doc_id)
    if not job.storage_key.startswith(document_prefix(business_id, doc_id)):
        raise _JobFailed("Source file key does not belong to this document")

    try:
        data = await storage.get_object(job.storage_key)
    except ObjectNotFoundError:
        raise _JobFailed("Source file is missing from storage") from None

    try:
        markdown = await asyncio.to_thread(extract_markdown, data, job.detected_mime)
    except ExtractionError as exc:
        raise _JobFailed(str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected extraction error for doc_id=%s", job.doc_id)
        raise _JobFailed("The document could not be read") from exc

    key = extracted_markdown_key(business_id, doc_id)
    await storage.put_object(key, markdown.encode("utf-8"), content_type="text/markdown; charset=utf-8")
    logger.info("Extracted doc_id=%s (%s, %d chars) to %s", job.doc_id, job.detected_mime, len(markdown), key)

    settings = get_settings()
    chunks = await asyncio.to_thread(
        chunk_markdown, markdown, size=settings.chunk_size, overlap=settings.chunk_overlap
    )
    key = chunks_key(business_id, doc_id)
    await storage.put_object(key, to_jsonl(chunks), content_type="application/x-ndjson")
    logger.info("Chunked doc_id=%s into %d chunks at %s", job.doc_id, len(chunks), key)

    try:
        await index_chunks(
            embedder=pipeline.embedder,
            store=pipeline.vector_store,
            business_id=job.business_id,
            doc_id=job.doc_id,
            chunks=chunks,
        )
    except EmbeddingError as exc:
        if exc.retryable:
            raise
        raise _JobFailed(f"The document could not be embedded: {exc}") from exc
    logger.info("Indexed doc_id=%s: %d vectors (%s)", job.doc_id, len(chunks), pipeline.embedder.model)


async def _report(
    channel: AbstractChannel, events_queue: str, job: IngestionJob, status: DocumentStatus, detail: str | None = None
) -> None:
    event = DocumentStatusEvent(
        doc_id=job.doc_id,
        business_id=job.business_id,
        status=status,
        occurred_at=datetime.now(UTC),
        detail=detail,
    )
    await publish_status(channel, events_queue, event)
    logger.info("Reported doc_id=%s business_id=%s -> %s", job.doc_id, job.business_id, status)


async def consume(
    url: str,
    queue_name: str,
    events_queue: str,
    *,
    pipeline: Pipeline | None = None,
    prefetch: int = 10,
    stop: asyncio.Event | None = None,
) -> None:
    """Consume `queue_name` until `stop` is set (or forever). Reconnects automatically."""
    pipeline = pipeline or Pipeline(storage=get_blob_storage(), embedder=get_embedder(), vector_store=get_vector_store())
    await pipeline.vector_store.ensure_collection(pipeline.embedder.dimensions)
    connection = await connect(url)
    async with connection:
        channel = await open_channel(connection, prefetch=prefetch)
        queue = await declare_queue(channel, queue_name)
        await declare_queue(channel, events_queue)
        await queue.consume(partial(handle_message, channel=channel, events_queue=events_queue, pipeline=pipeline))
        logger.info(
            "Consuming from queue %r (prefetch=%d), reporting to %r; embeddings: %s %s (%d dims)",
            queue_name,
            prefetch,
            events_queue,
            pipeline.embedder.provider,
            pipeline.embedder.model,
            pipeline.embedder.dimensions,
        )
        await (stop or asyncio.Event()).wait()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        asyncio.run(
            consume(
                settings.rabbitmq_url,
                settings.ingestion_queue,
                settings.document_events_queue,
                prefetch=settings.consumer_prefetch,
            )
        )
    except KeyboardInterrupt:
        logger.info("Consumer stopped")


if __name__ == "__main__":
    main()
