import json
import uuid
from datetime import UTC, datetime

import aio_pika
import pytest

from db.models import IngestionJob
from infra.queue import RabbitJobPublisher
from tests.workers.test_consumer import RABBITMQ_URL, _rabbitmq_reachable


def _job() -> IngestionJob:
    business_id, doc_id = uuid.uuid7(), uuid.uuid7()
    return IngestionJob(
        doc_id=doc_id,
        business_id=business_id,
        storage_key=f"{business_id}/{doc_id}/source.pdf",
        detected_mime="application/pdf",
        filename="report.pdf",
        size_bytes=1234,
        uploaded_at=datetime.now(UTC),
    )


async def test_publish_raises_when_rabbitmq_is_unreachable():
    publisher = RabbitJobPublisher("amqp://rve:rve@127.0.0.1:1/", "unused")
    with pytest.raises(Exception):
        await publisher.publish([_job()])
    await publisher.close()


@pytest.mark.skipif(not _rabbitmq_reachable(), reason=f"RabbitMQ not reachable at {RABBITMQ_URL}")
async def test_publishes_persistent_json_jobs():
    queue_name = f"test.publisher.{uuid.uuid4().hex}"
    publisher = RabbitJobPublisher(RABBITMQ_URL, queue_name)
    jobs = [_job(), _job()]
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        await publisher.publish(jobs)

        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True, passive=True)
        received = [await queue.get(timeout=5) for _ in jobs]

        for message, job in zip(received, jobs, strict=True):
            assert message.content_type == "application/json"
            assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
            assert message.message_id == str(job.doc_id)
            assert message.type == "ingestion.job"
            assert IngestionJob.model_validate_json(message.body) == job
            assert json.loads(message.body)["schema_version"] == 1
            await message.ack()
    finally:
        await publisher.close()
        cleanup = await connection.channel()
        await cleanup.queue_delete(queue_name)
        await connection.close()


@pytest.mark.skipif(not _rabbitmq_reachable(), reason=f"RabbitMQ not reachable at {RABBITMQ_URL}")
async def test_reconnects_after_the_connection_drops():
    queue_name = f"test.publisher.{uuid.uuid4().hex}"
    publisher = RabbitJobPublisher(RABBITMQ_URL, queue_name)
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        await publisher.publish([_job()])
        # Simulates RabbitMQ restarting underneath a long-lived API process.
        await publisher._connection.close()

        await publisher.publish([_job()])

        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True, passive=True)
        assert queue.declaration_result.message_count == 2
    finally:
        await publisher.close()
        cleanup = await connection.channel()
        await cleanup.queue_delete(queue_name)
        await connection.close()
