import asyncio
import uuid
from datetime import UTC, datetime

import aio_pika
import pytest
from sqlalchemy import pool, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.consumers import apply_status_event, consume_document_events
from db.models import Business, Document, DocumentStatus, DocumentStatusEvent
from tests.api.conftest import DATABASE_URL, run_db
from tests.workers.test_consumer import RABBITMQ_URL, _rabbitmq_reachable
from workers.publisher import publish_status


def _create_document() -> Document:
    async def create(session):
        business = Business(name="Acme")
        session.add(business)
        await session.flush()
        doc_id = uuid.uuid7()
        doc = Document(
            id=doc_id,
            business_id=business.id,
            filename="notes.txt",
            detected_mime="text/plain",
            size_bytes=35,
            storage_key=f"{business.id}/{doc_id}/source.txt",
        )
        session.add(doc)
        await session.commit()
        return doc

    return run_db(create)


def _status_of(doc_id: uuid.UUID) -> DocumentStatus:
    return run_db(lambda session: session.scalar(select(Document.status).where(Document.id == doc_id)))


def _detail_of(doc_id: uuid.UUID) -> str | None:
    return run_db(lambda session: session.scalar(select(Document.status_detail).where(Document.id == doc_id)))


def _event(
    doc: Document,
    *,
    business_id: uuid.UUID | None = None,
    status: DocumentStatus = DocumentStatus.INDEXED,
    detail: str | None = None,
) -> DocumentStatusEvent:
    return DocumentStatusEvent(
        doc_id=doc.id,
        business_id=business_id or doc.business_id,
        status=status,
        occurred_at=datetime.now(UTC),
        detail=detail,
    )


def test_failed_event_stores_the_reason_and_a_later_success_clears_it(migrated_db: None):
    doc = _create_document()
    reason = "The PDF has no text layer (scanned?); OCR is not available yet"

    run_db(lambda session: apply_status_event(session, _event(doc, status=DocumentStatus.FAILED, detail=reason)))
    assert (_status_of(doc.id), _detail_of(doc.id)) == (DocumentStatus.FAILED, reason)

    run_db(lambda session: apply_status_event(session, _event(doc, status=DocumentStatus.INDEXED)))
    assert (_status_of(doc.id), _detail_of(doc.id)) == (DocumentStatus.INDEXED, None)


def test_processing_moves_a_queued_document(migrated_db: None):
    doc = _create_document()
    assert run_db(lambda session: apply_status_event(session, _event(doc, status=DocumentStatus.PROCESSING)))
    assert _status_of(doc.id) is DocumentStatus.PROCESSING


@pytest.mark.parametrize("final", [DocumentStatus.INDEXED, DocumentStatus.FAILED])
def test_late_processing_event_does_not_overwrite_a_final_status(migrated_db: None, final: DocumentStatus):
    doc = _create_document()
    run_db(lambda session: apply_status_event(session, _event(doc, status=final)))

    assert run_db(lambda session: apply_status_event(session, _event(doc, status=DocumentStatus.PROCESSING))) is False
    assert _status_of(doc.id) is final


def test_apply_sets_status(migrated_db: None):
    doc = _create_document()
    assert _status_of(doc.id) is DocumentStatus.QUEUED

    assert run_db(lambda session: apply_status_event(session, _event(doc))) is True
    assert _status_of(doc.id) is DocumentStatus.INDEXED


def test_apply_ignores_event_for_another_business(migrated_db: None):
    doc = _create_document()

    assert run_db(lambda session: apply_status_event(session, _event(doc, business_id=uuid.uuid7()))) is False
    assert _status_of(doc.id) is DocumentStatus.QUEUED


def test_apply_ignores_unknown_document(migrated_db: None):
    event = DocumentStatusEvent(
        doc_id=uuid.uuid7(), business_id=uuid.uuid7(), status=DocumentStatus.INDEXED, occurred_at=datetime.now(UTC)
    )
    assert run_db(lambda session: apply_status_event(session, event)) is False


@pytest.mark.skipif(not _rabbitmq_reachable(), reason=f"RabbitMQ not reachable at {RABBITMQ_URL}")
async def test_consumer_applies_published_events(migrated_db: None):
    doc = await asyncio.to_thread(_create_document)
    queue_name = f"test.document.events.{uuid.uuid4().hex}"
    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    stop = asyncio.Event()
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        channel = await connection.channel()
        await channel.declare_queue(queue_name, durable=True)
        consumer = asyncio.create_task(
            consume_document_events(RABBITMQ_URL, queue_name, async_sessionmaker(engine), stop=stop)
        )

        await publish_status(channel, queue_name, _event(doc))

        deadline = asyncio.get_running_loop().time() + 10
        while await asyncio.to_thread(_status_of, doc.id) is not DocumentStatus.INDEXED:
            assert asyncio.get_running_loop().time() < deadline, "status was not applied"
            await asyncio.sleep(0.05)

        stop.set()
        await asyncio.wait_for(consumer, timeout=10)
        declared = await channel.declare_queue(queue_name, durable=True, passive=True)
        assert declared.declaration_result.message_count == 0
    finally:
        stop.set()
        cleanup = await connection.channel()
        await cleanup.queue_delete(queue_name)
        await connection.close()
        await engine.dispose()
