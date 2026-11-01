from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from db.models.schema import Document


class IngestionJob(BaseModel):
    """Payload published to INGESTION_QUEUE once a document's source file is in object storage.

    The worker fetches the file from `storage_key` and routes extraction by `detected_mime`.
    """

    schema_version: Literal[1] = 1
    doc_id: UUID
    business_id: UUID
    storage_key: str
    detected_mime: str
    filename: str
    size_bytes: int
    uploaded_at: datetime

    @classmethod
    def from_document(cls, doc: Document) -> "IngestionJob":
        return cls(
            doc_id=doc.id,
            business_id=doc.business_id,
            storage_key=doc.storage_key,
            detected_mime=doc.detected_mime,
            filename=doc.filename,
            size_bytes=doc.size_bytes,
            uploaded_at=doc.created_at,
        )
