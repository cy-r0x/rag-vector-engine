import asyncio
import os
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

import aio_pika
import pytest

from db.models import DocumentStatus, DocumentStatusEvent, IngestionJob
from workers.consumer import consume

RABBITMQ_URL = os.getenv("TEST_RABBITMQ_URL", "amqp://rve:rve@localhost:5672/")


def _rabbitmq_reachable() -> bool:
    parsed = urlparse(RABBITMQ_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 5672), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _rabbitmq_reachable(), reason=f"RabbitMQ not reachable at {RABBITMQ_URL}")


def _job() -> IngestionJob:
    business_id, doc_id = uuid.uuid7(), uuid.uuid7()
    return IngestionJob(
        doc_id=doc_id,
        business_id=business_id,
        storage_key=f"{business_id}/{doc_id}/source.txt",
        detected_mime="text/plain",
        filename="notes.txt",
        size_bytes=35,
        uploaded_at=datetime.now(UTC),
    )


@asynccontextmanager
async def _running_consumer():
    """A consumer on fresh queues; yields (channel, jobs_queue_name, events_queue_name)."""
    jobs_queue, events_queue = f"test.jobs.{uuid.uuid4().hex}", f"test.events.{uuid.uuid4().hex}"
    stop = asyncio.Event()
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    await channel.declare_queue(jobs_queue, durable=True)
    await channel.declare_queue(events_queue, durable=True)
    consumer = asyncio.create_task(consume(RABBITMQ_URL, jobs_queue, events_queue, stop=stop))
    try:
        yield channel, jobs_queue, events_queue
    finally:
        stop.set()
        await asyncio.wait_for(consumer, timeout=10)
        for name in (jobs_queue, events_queue):
            await channel.queue_delete(name)
        await connection.close()


async def _get(channel, queue_name: str, timeout: float = 10.0):
    queue = await channel.declare_queue(queue_name, durable=True, passive=True)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        message = await queue.get(fail=False)
        if message is not None:
            return message
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"No message on {queue_name}")
        await asyncio.sleep(0.05)


async def _message_count(channel, queue_name: str) -> int:
    declared = await channel.declare_queue(queue_name, durable=True, passive=True)
    return declared.declaration_result.message_count


async def test_job_is_acked_and_reported_as_indexed():
    job = _job()
    async with _running_consumer() as (channel, jobs_queue, events_queue):
        await channel.default_exchange.publish(
            aio_pika.Message(body=job.model_dump_json().encode(), message_id=str(job.doc_id)), routing_key=jobs_queue
        )

        message = await _get(channel, events_queue)
        event = DocumentStatusEvent.model_validate_json(message.body)
        await message.ack()

        assert (event.doc_id, event.business_id, event.status) == (job.doc_id, job.business_id, DocumentStatus.INDEXED)
        assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        assert await _message_count(channel, jobs_queue) == 0


async def test_malformed_job_is_dropped_without_an_event():
    async with _running_consumer() as (channel, jobs_queue, events_queue):
        await channel.default_exchange.publish(
            aio_pika.Message(body=b'{"doc_id": "not-a-uuid"}', message_id="bad-1"), routing_key=jobs_queue
        )

        await asyncio.sleep(0.5)
        assert await _message_count(channel, jobs_queue) == 0
        assert await _message_count(channel, events_queue) == 0
