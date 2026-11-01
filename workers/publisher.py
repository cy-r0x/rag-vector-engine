import aio_pika
from aio_pika.abc import AbstractChannel

from db.models import DocumentStatusEvent


async def publish_status(channel: AbstractChannel, queue_name: str, event: DocumentStatusEvent) -> None:
    """Publish a document status event; returns once RabbitMQ has confirmed it (the channel must use confirms)."""
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=event.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=f"{event.doc_id}:{event.status}",
            timestamp=event.occurred_at,
            type="document.status",
        ),
        routing_key=queue_name,
        mandatory=True,
    )
