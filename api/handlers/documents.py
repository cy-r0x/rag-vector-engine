import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid7

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from api.middleware.auth import get_business_id
from api.validators.file_type import (
    CSV,
    DOCX,
    PDF,
    TXT,
    XLSX,
    FileMetadata,
    FileTooLargeError,
    FileValidationError,
    detect_file_type,
)
from common.config import get_settings
from db import get_session
from db.models import Document, DocumentStatus, IngestionJob
from infra.queue import JobPublisher, get_job_publisher
from infra.storage.blob import (
    BlobStorage,
    ObjectNotFoundError,
    extracted_markdown_key,
    get_blob_storage,
    object_key,
)

logger = logging.getLogger("api.documents")

UPLOAD_FIELD = "files"
ALLOWED_MIMES = frozenset({PDF, DOCX, XLSX, CSV, TXT})

_MAX_FORM_FIELDS = 10
_STORE_CONCURRENCY = 8
_MAX_FILENAME_CHARS = 255
_FILENAME_JUNK = re.compile(r"[\x00-\x1f\x7f]")

UPLOAD_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD],
                    "properties": {
                        UPLOAD_FIELD: {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                            "maxItems": get_settings().max_upload_files,
                        }
                    },
                }
            }
        },
    }
}


class DocumentOut(BaseModel):
    doc_id: UUID
    filename: str
    status: DocumentStatus
    detected_mime: str
    size_bytes: int
    storage_key: str


class UploadResponse(BaseModel):
    documents: list[DocumentOut]


class DocumentSummary(BaseModel):
    doc_id: UUID
    filename: str
    status: DocumentStatus
    status_detail: str | None
    detected_mime: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]


async def list_documents(
    business_id: UUID = Depends(get_business_id),
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    """The caller's documents, newest first (UUIDv7 ids are time-ordered)."""
    rows = await session.scalars(
        select(Document).where(Document.business_id == business_id).order_by(Document.id.desc())
    )
    return DocumentListResponse(documents=[_to_summary(doc) for doc in rows])


async def get_document(
    doc_id: UUID,
    business_id: UUID = Depends(get_business_id),
    session: AsyncSession = Depends(get_session),
) -> DocumentSummary:
    return _to_summary(await _owned_document(session, business_id, doc_id))


async def get_document_markdown(
    doc_id: UUID,
    business_id: UUID = Depends(get_business_id),
    session: AsyncSession = Depends(get_session),
    storage: BlobStorage = Depends(get_blob_storage),
) -> Response:
    """The Markdown extracted from the document; available once it is INDEXED."""
    doc = await _owned_document(session, business_id, doc_id)
    if doc.status is not DocumentStatus.INDEXED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Markdown is available once the document is indexed (status: {doc.status})",
        )
    try:
        markdown = await storage.get_object(extracted_markdown_key(str(business_id), str(doc_id)))
    except ObjectNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Extracted Markdown not found for this document"
        ) from None
    return Response(content=markdown, media_type="text/markdown; charset=utf-8")


async def _owned_document(session: AsyncSession, business_id: UUID, doc_id: UUID) -> Document:
    """The caller's document; another business's document is indistinguishable from a missing one (404)."""
    doc = await session.scalar(select(Document).where(Document.id == doc_id, Document.business_id == business_id))
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc


def _to_summary(doc: Document) -> DocumentSummary:
    return DocumentSummary(
        doc_id=doc.id,
        filename=doc.filename,
        status=doc.status,
        status_detail=doc.status_detail,
        detected_mime=doc.detected_mime,
        size_bytes=doc.size_bytes,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


@dataclass(frozen=True)
class _ValidatedFile:
    upload: UploadFile
    filename: str
    meta: FileMetadata


async def upload_documents(
    request: Request,
    business_id: UUID = Depends(get_business_id),
    session: AsyncSession = Depends(get_session),
    storage: BlobStorage = Depends(get_blob_storage),
    publisher: JobPublisher = Depends(get_job_publisher),
) -> UploadResponse | JSONResponse:
    """Accept up to `max_upload_files` documents as multipart field `files`; all or nothing.

    The form is parsed here rather than by FastAPI so that auth and the size/count limits
    are enforced before the body is read.
    """
    settings = get_settings()
    _check_content_length(request, settings.max_batch_bytes)

    async with request.form(max_files=settings.max_upload_files, max_fields=_MAX_FORM_FIELDS) as form:
        uploads = form.getlist(UPLOAD_FIELD)
        if not uploads:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"No files uploaded; send them as multipart field '{UPLOAD_FIELD}'",
            )
        if not all(isinstance(item, UploadFile) for item in uploads):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Every '{UPLOAD_FIELD}' part must be a file",
            )

        validated, errors = await _validate(uploads, settings.max_upload_bytes)
        if errors:
            all_too_large = all([error.pop("too_large") for error in errors])
            return JSONResponse(
                status_code=(
                    status.HTTP_413_CONTENT_TOO_LARGE if all_too_large else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
                ),
                content={
                    "detail": f"{len(errors)} of {len(uploads)} files were rejected; nothing was stored",
                    "errors": errors,
                },
            )

        documents = [_new_document(business_id, item) for item in validated]
        await _store_all(storage, validated, documents)

    session.add_all(documents)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await _delete_quietly(storage, [doc.storage_key for doc in documents])
        raise

    try:
        await publisher.publish([IngestionJob.from_document(doc) for doc in documents])
    except Exception:
        logger.exception("Failed to queue %d documents for business %s; discarding the batch", len(documents), business_id)
        await _discard_batch(session, storage, documents)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Documents could not be queued for processing; nothing was stored. Please try again.",
        ) from None

    logger.info("Stored and queued %d documents for business %s", len(documents), business_id)
    return UploadResponse(documents=[_to_out(doc) for doc in documents])


def _check_content_length(request: Request, max_bytes: int) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        raise HTTPException(status_code=status.HTTP_411_LENGTH_REQUIRED, detail="Content-Length header is required")
    try:
        length = int(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length header") from None
    if length > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Request body exceeds the maximum of {max_bytes} bytes",
        )


async def _validate(
    uploads: list[UploadFile], max_file_bytes: int
) -> tuple[list[_ValidatedFile], list[dict[str, Any]]]:
    validated: list[_ValidatedFile] = []
    errors: list[dict[str, Any]] = []
    for index, upload in enumerate(uploads):
        filename = _clean_filename(upload.filename)
        try:
            if upload.size is not None and upload.size > max_file_bytes:
                raise FileTooLargeError(f"File exceeds the maximum size of {max_file_bytes} bytes")
            data = await upload.read()
            meta = await run_in_threadpool(
                detect_file_type, data, filename=filename, max_size=max_file_bytes, allowed=ALLOWED_MIMES
            )
        except FileValidationError as exc:
            errors.append(
                {
                    "index": index,
                    "filename": filename,
                    "detail": exc.detail,
                    "detected_mime": exc.detected_mime,
                    "too_large": isinstance(exc, FileTooLargeError),
                }
            )
            continue
        validated.append(_ValidatedFile(upload=upload, filename=filename, meta=meta))
    return validated, errors


def _clean_filename(raw: str | None) -> str:
    """Keep only the base name, for display; storage keys never use it."""
    name = _FILENAME_JUNK.sub("", (raw or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:_MAX_FILENAME_CHARS] or "untitled"


def _new_document(business_id: UUID, item: _ValidatedFile) -> Document:
    doc_id = uuid7()
    return Document(
        id=doc_id,
        business_id=business_id,
        filename=item.filename,
        detected_mime=item.meta.mime,
        size_bytes=item.meta.size,
        storage_key=object_key(str(business_id), str(doc_id), item.meta.extension),
        status=DocumentStatus.QUEUED,
    )


async def _store_all(storage: BlobStorage, validated: list[_ValidatedFile], documents: list[Document]) -> None:
    semaphore = asyncio.Semaphore(_STORE_CONCURRENCY)

    async def store(item: _ValidatedFile, doc: Document) -> None:
        async with semaphore:
            await item.upload.seek(0)
            await storage.put_object(doc.storage_key, await item.upload.read(), content_type=item.meta.mime)

    results = await asyncio.gather(
        *(store(item, doc) for item, doc in zip(validated, documents, strict=True)), return_exceptions=True
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        logger.error("Failed to store %d of %d documents", len(failures), len(documents), exc_info=failures[0])
        await _delete_quietly(storage, [doc.storage_key for doc in documents])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to store documents")


async def _discard_batch(session: AsyncSession, storage: BlobStorage, documents: list[Document]) -> None:
    """Undo a committed batch: its rows first, so no document is listed without a source file."""
    try:
        await session.execute(delete(Document).where(Document.id.in_([doc.id for doc in documents])))
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Could not delete rows of discarded batch %s", [str(doc.id) for doc in documents])
        return
    await _delete_quietly(storage, [doc.storage_key for doc in documents])


async def _delete_quietly(storage: BlobStorage, keys: list[str]) -> None:
    for key in keys:
        try:
            await storage.delete_object(key)
        except Exception:
            logger.warning("Could not delete orphaned object %s", key, exc_info=True)


def _to_out(doc: Document) -> DocumentOut:
    return DocumentOut(
        doc_id=doc.id,
        filename=doc.filename,
        status=doc.status,
        detected_mime=doc.detected_mime,
        size_bytes=doc.size_bytes,
        storage_key=doc.storage_key,
    )
