import asyncio
import logging
from datetime import UTC, datetime
from functools import partial

from aio_pika.abc import AbstractChannel, AbstractIncomingMessage
from pydantic import ValidationError

from common.config import get_settings
from common.logger import setup_logging
from db.models import DocumentStatus, DocumentStatusEvent, IngestionJob
from infra.queue import connect, declare_queue, open_channel
from workers.publisher import publish_status

logger = logging.getLogger("workers.consumer")

_MAX_LOGGED_BODY_CHARS = 2000


async def handle_message(message: AbstractIncomingMessage, *, channel: AbstractChannel, events_queue: str) -> None:
    """Process one ingestion job and report its status.

    A malformed job is rejected without requeue; if the status event can't be published the job is requeued,
    so it is only acked once its outcome has been recorded on the events queue.
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

    # Placeholder until the extraction → chunking → embedding pipeline exists: every job is reported as indexed.
    event = DocumentStatusEvent(
        doc_id=job.doc_id,
        business_id=job.business_id,
        status=DocumentStatus.INDEXED,
        occurred_at=datetime.now(UTC),
    )
    try:
        await publish_status(channel, events_queue, event)
    except Exception:
        logger.exception("Could not publish status for doc_id=%s; requeueing the job", job.doc_id)
        await message.nack(requeue=True)
        return

    await message.ack()
    logger.info("Processed doc_id=%s business_id=%s -> %s", job.doc_id, job.business_id, event.status)


async def consume(
    url: str,
    queue_name: str,
    events_queue: str,
    *,
    prefetch: int = 10,
    stop: asyncio.Event | None = None,
) -> None:
    """Consume `queue_name` until `stop` is set (or forever). Reconnects automatically."""
    connection = await connect(url)
    async with connection:
        channel = await open_channel(connection, prefetch=prefetch)
        queue = await declare_queue(channel, queue_name)
        await declare_queue(channel, events_queue)
        await queue.consume(partial(handle_message, channel=channel, events_queue=events_queue))
        logger.info("Consuming from queue %r (prefetch=%d), reporting to %r", queue_name, prefetch, events_queue)
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
