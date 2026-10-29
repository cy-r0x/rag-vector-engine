import asyncio
import logging
import os
import socket
import uuid
from urllib.parse import urlparse

import aio_pika
import pytest

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


async def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.05)


async def test_consumes_logs_and_acks(caplog: pytest.LogCaptureFixture):
    queue_name = f"test.consumer.{uuid.uuid4().hex}"
    stop = asyncio.Event()
    caplog.set_level(logging.INFO, logger="workers.consumer")

    consumer = asyncio.create_task(consume(RABBITMQ_URL, queue_name, stop=stop))
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        channel = await connection.channel()
        await channel.declare_queue(queue_name, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(body=b'{"doc_id": "doc_1"}', message_id="msg-1"), routing_key=queue_name
        )

        await _wait_for(lambda: any("msg-1" in r.getMessage() for r in caplog.records))
        record = next(r for r in caplog.records if "msg-1" in r.getMessage())
        assert '{"doc_id": "doc_1"}' in record.getMessage()

        stop.set()
        await asyncio.wait_for(consumer, timeout=10)

        # Unacked messages return to the queue when the consumer disconnects, so an empty queue proves the ack.
        declared = await channel.declare_queue(queue_name, durable=True, passive=True)
        assert declared.declaration_result.message_count == 0
    finally:
        stop.set()
        if not consumer.done():
            await asyncio.wait_for(consumer, timeout=10)
        cleanup = await connection.channel()
        await cleanup.queue_delete(queue_name)
        await connection.close()
