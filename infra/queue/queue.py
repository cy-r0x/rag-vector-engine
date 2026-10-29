import aio_pika
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection


async def connect(url: str) -> AbstractRobustConnection:
    """Open a RabbitMQ connection that reconnects automatically."""
    return await aio_pika.connect_robust(url)


async def open_channel(connection: AbstractRobustConnection, *, prefetch: int | None = None) -> AbstractChannel:
    channel = await connection.channel()
    if prefetch is not None:
        await channel.set_qos(prefetch_count=prefetch)
    return channel


async def declare_queue(channel: AbstractChannel, name: str) -> AbstractQueue:
    """Declare a queue with the project's standard arguments.

    Publishers and consumers must both use this: RabbitMQ rejects redeclaring a queue with different arguments.
    """
    return await channel.declare_queue(name, durable=True)
