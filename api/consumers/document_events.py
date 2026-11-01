import asyncio
import logging
from functools import partial

from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.config import get_settings
from db import get_sessionmaker
from db.models import Document, DocumentStatus, DocumentStatusEvent
from infra.queue import connect, declare_queue, open_channel

logger = logging.getLogger("api.document_events")

_RETRY_SECONDS = 5


async def apply_status_event(session: AsyncSession, event: DocumentStatusEvent) -> bool:
    """Set the document's status and status detail (the failure reason, cleared by any other status).

    Returns False if no document matches the event's doc_id and business_id.

    Events can be applied out of order (messages are handled concurrently), so PROCESSING only moves a document
    out of QUEUED and never overwrites a final INDEXED/FAILED.
    """
    statement = update(Document).where(Document.id == event.doc_id, Document.business_id == event.business_id)
    if event.status is DocumentStatus.PROCESSING:
        statement = statement.where(Document.status == DocumentStatus.QUEUED)
    result = await session.execute(statement.values(status=event.status, status_detail=event.detail))
    await session.commit()
    return result.rowcount > 0


async def handle_event(message: AbstractIncomingMessage, *, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    try:
        event = DocumentStatusEvent.model_validate_json(message.body)
    except ValidationError as exc:
        logger.error("Rejecting malformed status event id=%s: %s", message.message_id, exc)
        await message.reject(requeue=False)
        return

    try:
        async with sessionmaker() as session:
            applied = await apply_status_event(session, event)
    except Exception:
        logger.exception("Could not apply status event for doc_id=%s; requeueing", event.doc_id)
        await asyncio.sleep(_RETRY_SECONDS)
        await message.nack(requeue=True)
        return

    await message.ack()
    if applied:
        logger.info("Document %s -> %s%s", event.doc_id, event.status, f" ({event.detail})" if event.detail else "")
    else:
        logger.warning(
            "Ignored %s event for document %s (business %s): unknown document or stale event",
            event.status,
            event.doc_id,
            event.business_id,
        )


async def consume_document_events(
    url: str,
    queue_name: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    prefetch: int = 10,
    stop: asyncio.Event,
) -> None:
    """Apply status events from `queue_name` until `stop` is set. Reconnects automatically once connected."""
    connection = await connect(url)
    async with connection:
        channel = await open_channel(connection, prefetch=prefetch)
        queue = await declare_queue(channel, queue_name)
        await queue.consume(partial(handle_event, sessionmaker=sessionmaker))
        logger.info("Consuming document status events from %r", queue_name)
        await stop.wait()


async def run_document_events_consumer(stop: asyncio.Event) -> None:
    """Background task for the API process; keeps retrying the first connection so the API starts without RabbitMQ."""
    settings = get_settings()
    while not stop.is_set():
        try:
            await consume_document_events(
                settings.rabbitmq_url,
                settings.document_events_queue,
                get_sessionmaker(),
                prefetch=settings.consumer_prefetch,
                stop=stop,
            )
        except Exception as exc:
            logger.warning("Document events consumer unavailable (%s); retrying in %ds", exc, _RETRY_SECONDS)
            try:
                await asyncio.wait_for(stop.wait(), _RETRY_SECONDS)
            except TimeoutError:
                pass
