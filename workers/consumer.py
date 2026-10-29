import asyncio
import logging

from aio_pika.abc import AbstractIncomingMessage

from common.config import get_settings
from common.logger import setup_logging
from infra.queue import connect, declare_queue, open_channel

logger = logging.getLogger("workers.consumer")

_MAX_LOGGED_BODY_CHARS = 2000


async def handle_message(message: AbstractIncomingMessage) -> None:
    """Acks on success; on any exception the message is rejected without requeue."""
    async with message.process(requeue=False):
        body = message.body.decode("utf-8", errors="replace")
        logger.info(
            "Received message id=%s routing_key=%s size=%d body=%s",
            message.message_id,
            message.routing_key,
            len(message.body),
            body[:_MAX_LOGGED_BODY_CHARS],
        )


async def consume(url: str, queue_name: str, *, prefetch: int = 10, stop: asyncio.Event | None = None) -> None:
    """Consume `queue_name` until `stop` is set (or forever). Reconnects automatically."""
    connection = await connect(url)
    async with connection:
        channel = await open_channel(connection, prefetch=prefetch)
        queue = await declare_queue(channel, queue_name)
        await queue.consume(handle_message)
        logger.info("Consuming from queue %r (prefetch=%d)", queue_name, prefetch)
        await (stop or asyncio.Event()).wait()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        asyncio.run(consume(settings.rabbitmq_url, settings.ingestion_queue, prefetch=settings.consumer_prefetch))
    except KeyboardInterrupt:
        logger.info("Consumer stopped")


if __name__ == "__main__":
    main()
