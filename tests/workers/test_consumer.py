import asyncio
import json
import os
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

import aio_pika
import pytest

from api.validators.file_type import PDF, TXT
from db.models import DocumentStatus, DocumentStatusEvent, IngestionJob
from infra.storage.blob import chunks_key, extracted_markdown_key
from tests.api.conftest import InMemoryBlobStorage
from tests.workers.sample_documents import blank_pdf
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


def _job(mime: str = TXT, ext: str = "txt") -> IngestionJob:
    business_id, doc_id = uuid.uuid7(), uuid.uuid7()
    return IngestionJob(
        doc_id=doc_id,
        business_id=business_id,
        storage_key=f"{business_id}/{doc_id}/source.{ext}",
        detected_mime=mime,
        filename=f"file.{ext}",
        size_bytes=35,
        uploaded_at=datetime.now(UTC),
    )


@asynccontextmanager
async def _running_consumer(storage: InMemoryBlobStorage):
    """A consumer on fresh queues; yields (channel, jobs_queue_name, events_queue_name)."""
    jobs_queue, events_queue = f"test.jobs.{uuid.uuid4().hex}", f"test.events.{uuid.uuid4().hex}"
    stop = asyncio.Event()
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    await channel.declare_queue(jobs_queue, durable=True)
    await channel.declare_queue(events_queue, durable=True)
    consumer = asyncio.create_task(consume(RABBITMQ_URL, jobs_queue, events_queue, storage=storage, stop=stop))
    try:
        yield channel, jobs_queue, events_queue
    finally:
        stop.set()
        await asyncio.wait_for(consumer, timeout=10)
        for name in (jobs_queue, events_queue):
            await channel.queue_delete(name)
        await connection.close()


async def _publish(channel, queue_name: str, job: IngestionJob) -> None:
    await channel.default_exchange.publish(
        aio_pika.Message(body=job.model_dump_json().encode(), message_id=str(job.doc_id)), routing_key=queue_name
    )


async def _events(channel, queue_name: str, count: int, timeout: float = 10.0) -> list[DocumentStatusEvent]:
    queue = await channel.declare_queue(queue_name, durable=True, passive=True)
    deadline = asyncio.get_running_loop().time() + timeout
    events = []
    while len(events) < count:
        message = await queue.get(fail=False)
        if message is None:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"Got {len(events)} of {count} events on {queue_name}")
            await asyncio.sleep(0.05)
            continue
        assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        events.append(DocumentStatusEvent.model_validate_json(message.body))
        await message.ack()
    return events


async def _message_count(channel, queue_name: str) -> int:
    declared = await channel.declare_queue(queue_name, durable=True, passive=True)
    return declared.declaration_result.message_count


async def test_text_is_extracted_stored_and_reported_as_indexed():
    storage = InMemoryBlobStorage()
    job = _job()
    await storage.put_object(job.storage_key, b"Quarterly notes\n\nRevenue grew 12%.\n", TXT)

    async with _running_consumer(storage) as (channel, jobs_queue, events_queue):
        await _publish(channel, jobs_queue, job)
        events = await _events(channel, events_queue, 2)

        assert [event.status for event in events] == [DocumentStatus.PROCESSING, DocumentStatus.INDEXED]
        assert {(event.doc_id, event.business_id) for event in events} == {(job.doc_id, job.business_id)}
        key = extracted_markdown_key(str(job.business_id), str(job.doc_id))
        assert storage.objects[key] == (b"Quarterly notes\n\nRevenue grew 12%.\n", "text/markdown; charset=utf-8")
        body, content_type = storage.objects[chunks_key(str(job.business_id), str(job.doc_id))]
        assert content_type == "application/x-ndjson"
        assert [json.loads(line) for line in body.decode().splitlines()] == [
            {"index": 0, "text": "Quarterly notes\n\nRevenue grew 12%.", "start": 0, "end": 34}
        ]
        assert await _message_count(channel, jobs_queue) == 0


async def test_scanned_pdf_is_reported_as_failed_with_reason():
    storage = InMemoryBlobStorage()
    job = _job(PDF, "pdf")
    await storage.put_object(job.storage_key, blank_pdf(), PDF)

    async with _running_consumer(storage) as (channel, jobs_queue, events_queue):
        await _publish(channel, jobs_queue, job)
        events = await _events(channel, events_queue, 2)

        assert [event.status for event in events] == [DocumentStatus.PROCESSING, DocumentStatus.FAILED]
        assert "no text layer" in events[1].detail
        assert extracted_markdown_key(str(job.business_id), str(job.doc_id)) not in storage.objects
        assert chunks_key(str(job.business_id), str(job.doc_id)) not in storage.objects
        assert await _message_count(channel, jobs_queue) == 0


async def test_missing_source_is_reported_as_failed():
    async with _running_consumer(InMemoryBlobStorage()) as (channel, jobs_queue, events_queue):
        await _publish(channel, jobs_queue, _job())
        events = await _events(channel, events_queue, 2)

        assert events[1].status is DocumentStatus.FAILED
        assert events[1].detail == "Source file is missing from storage"


async def test_malformed_job_is_dropped_without_an_event():
    async with _running_consumer(InMemoryBlobStorage()) as (channel, jobs_queue, events_queue):
        await channel.default_exchange.publish(
            aio_pika.Message(body=b'{"doc_id": "not-a-uuid"}', message_id="bad-1"), routing_key=jobs_queue
        )

        await asyncio.sleep(0.5)
        assert await _message_count(channel, jobs_queue) == 0
        assert await _message_count(channel, events_queue) == 0
