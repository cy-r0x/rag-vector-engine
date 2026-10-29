import io
import os
import urllib.request
import uuid

import pytest

from storage.blob import ObjectNotFoundError, S3BlobStorage, object_key

ENDPOINT = os.getenv("TEST_S3_ENDPOINT_URL", "http://localhost:9000")
ACCESS_KEY = os.getenv("TEST_S3_ACCESS_KEY_ID", "minioadmin")
SECRET_KEY = os.getenv("TEST_S3_SECRET_ACCESS_KEY", "minioadmin")
BUCKET = "documents-test"


def _minio_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/minio/health/live", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


requires_minio = pytest.mark.skipif(not _minio_reachable(), reason=f"MinIO not reachable at {ENDPOINT}")


@pytest.fixture
async def storage() -> S3BlobStorage:
    store = S3BlobStorage(
        bucket=BUCKET,
        endpoint_url=ENDPOINT,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        force_path_style=True,
    )
    await store.ensure_bucket()
    return store


@pytest.fixture
async def prefix(storage: S3BlobStorage):
    value = f"test-{uuid.uuid4().hex}/"
    yield value
    await storage.delete_prefix(value)


def test_object_key_builds_tenant_scoped_path():
    assert object_key("biz_98a72b1c", "doc_e4c3a102", "pdf") == "biz_98a72b1c/doc_e4c3a102/source.pdf"


@pytest.mark.parametrize(
    ("business_id", "doc_id", "ext"),
    [
        ("", "doc_1", "pdf"),
        ("tnt/1", "doc_1", "pdf"),
        ("..", "doc_1", "pdf"),
        ("biz_1", "../other", "pdf"),
        ("biz_1", "doc_1", ""),
        ("biz_1", "doc_1", "pdf/../x"),
        ("biz_1", "doc_1", "PDF"),
    ],
)
def test_object_key_rejects_unsafe_input(business_id: str, doc_id: str, ext: str):
    with pytest.raises(ValueError):
        object_key(business_id, doc_id, ext)


@requires_minio
async def test_ensure_bucket_is_idempotent(storage: S3BlobStorage):
    await storage.ensure_bucket()


@requires_minio
async def test_put_get_roundtrip_bytes(storage: S3BlobStorage, prefix: str):
    key = f"{prefix}a.pdf"
    await storage.put_object(key, b"%PDF-1.7 hello", "application/pdf", {"doc-id": "doc_1"})

    assert await storage.get_object(key) == b"%PDF-1.7 hello"
    info = await storage.head_object(key)
    assert info.size == len(b"%PDF-1.7 hello")
    assert info.content_type == "application/pdf"
    assert info.metadata == {"doc-id": "doc_1"}


@requires_minio
async def test_put_get_roundtrip_file_like(storage: S3BlobStorage, prefix: str):
    key = f"{prefix}b.csv"
    payload = b"a,b\n1,2\n" * 1000
    await storage.put_object(key, io.BytesIO(payload), "text/csv")

    assert await storage.get_object(key) == payload
    assert (await storage.head_object(key)).content_type == "text/csv"


@requires_minio
async def test_exists(storage: S3BlobStorage, prefix: str):
    key = f"{prefix}c.txt"
    assert not await storage.exists(key)
    await storage.put_object(key, b"x", "text/plain")
    assert await storage.exists(key)


@requires_minio
async def test_missing_object_raises_not_found(storage: S3BlobStorage, prefix: str):
    with pytest.raises(ObjectNotFoundError):
        await storage.get_object(f"{prefix}missing")
    with pytest.raises(ObjectNotFoundError):
        await storage.head_object(f"{prefix}missing")


@requires_minio
async def test_delete_object(storage: S3BlobStorage, prefix: str):
    key = f"{prefix}d.txt"
    await storage.put_object(key, b"x", "text/plain")
    await storage.delete_object(key)
    assert not await storage.exists(key)


@requires_minio
async def test_delete_prefix_only_deletes_under_prefix(storage: S3BlobStorage, prefix: str):
    target = f"{prefix}tenant_a/"
    sibling = f"{prefix}tenant_ab/keep.txt"
    for i in range(3):
        await storage.put_object(f"{target}{i}.txt", b"x", "text/plain")
    await storage.put_object(sibling, b"x", "text/plain")

    assert await storage.delete_prefix(target) == 3
    assert not await storage.exists(f"{target}0.txt")
    assert await storage.exists(sibling)


async def test_delete_prefix_rejects_empty_prefix():
    store = S3BlobStorage(bucket=BUCKET, endpoint_url=ENDPOINT, force_path_style=True)
    with pytest.raises(ValueError):
        await store.delete_prefix("")
