import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection

from common.config import get_settings
from db.models import IngestionJob
from infra.queue.queue import declare_queue

logger = logging.getLogger("infra.queue.publisher")

_PUBLISH_TIMEOUT_SECONDS = 10


class JobPublisher(Protocol):
    async def publish(self, jobs: Sequence[IngestionJob]) -> None:
        """Publish every job or raise; a job is published once RabbitMQ has confirmed it."""
        ...

    async def close(self) -> None: ...


class RabbitJobPublisher:
    """Publishes ingestion jobs as persistent JSON messages to the ingestion queue, with publisher confirms.

    Uses a plain (non-robust) connection opened on demand: while RabbitMQ is down a publish fails fast
    instead of waiting on a background reconnect loop, and the first publish after it is back reconnects.
    The API therefore starts even while RabbitMQ is down.
    """

    def __init__(self, url: str, queue_name: str) -> None:
        self._url = url
        self._queue_name = queue_name
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._lock = asyncio.Lock()

    async def _ensure_channel(self) -> AbstractChannel:
        async with self._lock:
            if self._channel is None or self._channel.is_closed:
                if self._connection is None or self._connection.is_closed:
                    self._connection = await aio_pika.connect(self._url, timeout=_PUBLISH_TIMEOUT_SECONDS)
                self._channel = await self._connection.channel(publisher_confirms=True, on_return_raises=True)
                await declare_queue(self._channel, self._queue_name)
            return self._channel

    async def publish(self, jobs: Sequence[IngestionJob]) -> None:
        if not jobs:
            return
        try:
            channel = await self._ensure_channel()
            now = datetime.now(UTC)
            await asyncio.gather(
                *(
                    channel.default_exchange.publish(
                        aio_pika.Message(
                            body=job.model_dump_json().encode(),
                            content_type="application/json",
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            message_id=str(job.doc_id),
                            timestamp=now,
                            type="ingestion.job",
                        ),
                        routing_key=self._queue_name,
                        mandatory=True,
                        timeout=_PUBLISH_TIMEOUT_SECONDS,
                    )
                    for job in jobs
                )
            )
        except Exception:
            await self.close()
            raise
        logger.info("Published %d ingestion jobs to %r", len(jobs), self._queue_name)

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection, self._channel = self._connection, None, None
            if connection is not None and not connection.is_closed:
                try:
                    await connection.close()
                except Exception:
                    logger.debug("Error while closing RabbitMQ connection", exc_info=True)


@lru_cache
def get_job_publisher() -> JobPublisher:
    settings = get_settings()
    return RabbitJobPublisher(settings.rabbitmq_url, settings.ingestion_queue)


async def close_job_publisher() -> None:
    if get_job_publisher.cache_info().currsize:
        await get_job_publisher().close()
