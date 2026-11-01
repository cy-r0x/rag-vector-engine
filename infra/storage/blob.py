import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import BinaryIO, Protocol

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common.config import Settings, get_settings

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXT_PATTERN = re.compile(r"^[a-z0-9]{1,10}$")
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}
_NO_BUCKET_CODES = {"404", "NoSuchBucket", "NotFound"}
_DELETE_BATCH_SIZE = 1000


class ObjectNotFoundError(LookupError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Object not found: {key}")
        self.key = key


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    content_type: str
    metadata: dict[str, str] = field(default_factory=dict)


class BlobStorage(Protocol):
    async def ensure_bucket(self) -> None: ...

    async def put_object(
        self,
        key: str,
        body: bytes | BinaryIO,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None: ...

    async def get_object(self, key: str) -> bytes: ...

    async def head_object(self, key: str) -> ObjectInfo: ...

    async def exists(self, key: str) -> bool: ...

    async def delete_object(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...


def object_key(business_id: str, doc_id: str, ext: str) -> str:
    """Build `{business_id}/{doc_id}/source.{ext}`; rejects anything that could escape the tenant prefix."""
    if not _EXT_PATTERN.fullmatch(ext):
        raise ValueError(f"Invalid extension: {ext!r}")
    return f"{document_prefix(business_id, doc_id)}source.{ext}"


def extracted_markdown_key(business_id: str, doc_id: str) -> str:
    """Build `{business_id}/{doc_id}/extracted.md`, where the worker stores a document's extracted Markdown."""
    return f"{document_prefix(business_id, doc_id)}extracted.md"


def chunks_key(business_id: str, doc_id: str) -> str:
    """Build `{business_id}/{doc_id}/chunks.jsonl`, where the worker stores a document's chunks (one JSON per line)."""
    return f"{document_prefix(business_id, doc_id)}chunks.jsonl"


def document_prefix(business_id: str, doc_id: str) -> str:
    """Build `{business_id}/{doc_id}/`, the prefix holding every object of one document."""
    if not _ID_PATTERN.fullmatch(business_id):
        raise ValueError(f"Invalid business_id: {business_id!r}")
    if not _ID_PATTERN.fullmatch(doc_id):
        raise ValueError(f"Invalid doc_id: {doc_id!r}")
    return f"{business_id}/{doc_id}/"


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class S3BlobStorage:
    """BlobStorage over the S3 API; works with AWS S3 and S3-compatible servers such as MinIO."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        force_path_style: bool = False,
    ) -> None:
        self.bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._session = aioboto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )
        self._config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if force_path_style else "virtual"},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3BlobStorage":
        return cls(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            force_path_style=settings.s3_force_path_style,
        )

    def _client(self):
        return self._session.client("s3", endpoint_url=self._endpoint_url, config=self._config)

    async def ensure_bucket(self) -> None:
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self.bucket)
                return
            except ClientError as exc:
                if _error_code(exc) not in _NO_BUCKET_CODES:
                    raise
            params: dict = {"Bucket": self.bucket}
            if self._region != "us-east-1":
                params["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
            await s3.create_bucket(**params)

    async def put_object(
        self,
        key: str,
        body: bytes | BinaryIO,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        fileobj = io.BytesIO(body) if isinstance(body, bytes) else body
        extra_args: dict = {"ContentType": content_type}
        if metadata:
            extra_args["Metadata"] = dict(metadata)
        async with self._client() as s3:
            await s3.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra_args)

    async def get_object(self, key: str) -> bytes:
        async with self._client() as s3:
            try:
                response = await s3.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    raise ObjectNotFoundError(key) from exc
                raise
            async with response["Body"] as stream:
                return await stream.read()

    async def head_object(self, key: str) -> ObjectInfo:
        async with self._client() as s3:
            try:
                response = await s3.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    raise ObjectNotFoundError(key) from exc
                raise
        return ObjectInfo(
            key=key,
            size=response["ContentLength"],
            content_type=response.get("ContentType", "application/octet-stream"),
            metadata=response.get("Metadata", {}),
        )

    async def exists(self, key: str) -> bool:
        try:
            await self.head_object(key)
        except ObjectNotFoundError:
            return False
        return True

    async def delete_object(self, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> int:
        if not prefix:
            raise ValueError("Refusing to delete with an empty prefix")
        deleted = 0
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                for start in range(0, len(keys), _DELETE_BATCH_SIZE):
                    batch = keys[start : start + _DELETE_BATCH_SIZE]
                    await s3.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
                    deleted += len(batch)
        return deleted


@lru_cache
def get_blob_storage() -> BlobStorage:
    return S3BlobStorage.from_settings(get_settings())
