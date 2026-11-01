import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.handlers.documents import _clean_filename
from api.main import app
from common.auth import get_jwt_codec
from common.config import get_settings
from db.models import Document, DocumentStatus
from tests.api.conftest import InMemoryBlobStorage, run_db
from tests.api.test_file_type import DOCX_BYTES, PDF_BYTES, PNG_BYTES, XLSX_BYTES

URL = "/v1/documents/upload"
CSV_BYTES = b"name,amount\nacme,100\nglobex,200\n"
TXT_BYTES = b"Quarterly notes\n\nRevenue grew 12%.\n"

ONE_OF_EACH = [
    ("report.pdf", PDF_BYTES, "application/pdf"),
    ("letter.docx", DOCX_BYTES, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("sheet.xlsx", XLSX_BYTES, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("data.csv", CSV_BYTES, "text/csv"),
    ("notes.txt", TXT_BYTES, "text/plain"),
]


def _auth(business_id: str = "biz_test0000") -> dict[str, str]:
    return {"Authorization": f"Bearer {get_jwt_codec().encode(sub='usr_test0000', business_id=business_id)}"}


def _files(*items: tuple[str, bytes]) -> list[tuple[str, tuple[str, bytes, str]]]:
    # The declared content type is deliberately wrong: only the bytes may decide.
    return [("files", (name, data, "application/octet-stream")) for name, data in items]


@pytest.fixture
def app_client(blob_storage: InMemoryBlobStorage) -> TestClient:
    """Client for requests rejected before the database is touched."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def business(client: TestClient) -> dict:
    response = client.post(
        "/v1/auth/register",
        json={"business_name": "Acme", "email": f"{uuid.uuid4().hex[:12]}@acme.com", "password": "s3cret-passw0rd"},
    )
    assert response.status_code == 201
    data = response.json()
    return {"business_id": data["business_id"], "headers": {"Authorization": f"Bearer {data['access_token']}"}}


def _documents_of(business_id: str) -> list[Document]:
    async def fetch(session):
        return list(await session.scalars(select(Document).where(Document.business_id == business_id)))

    return run_db(fetch)


def test_requires_token(app_client: TestClient):
    assert app_client.post(URL, files=_files(("a.pdf", PDF_BYTES))).status_code == 401


def test_requires_content_length(app_client: TestClient):
    response = app_client.post(
        URL,
        content=iter([b"--x\r\n"]),
        headers={**_auth(), "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 411


def test_rejects_request_over_batch_cap(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "max_batch_bytes", 1_000)
    response = app_client.post(URL, headers=_auth(), files=_files(("a.txt", b"x" * 2_000)))
    assert response.status_code == 413


def test_rejects_more_than_max_files(app_client: TestClient, blob_storage: InMemoryBlobStorage):
    files = _files(*[(f"n{i}.txt", TXT_BYTES) for i in range(get_settings().max_upload_files + 1)])
    response = app_client.post(URL, headers=_auth(), files=files)
    assert response.status_code == 400
    assert blob_storage.objects == {}


def test_rejects_missing_files_field(app_client: TestClient):
    response = app_client.post(URL, headers=_auth(), files=[("other", ("a.txt", TXT_BYTES, "text/plain"))])
    assert response.status_code == 422


def test_rejects_text_part_in_files_field(app_client: TestClient):
    response = app_client.post(URL, headers=_auth(), data={"files": "not a file"}, files=_files(("a.txt", TXT_BYTES)))
    assert response.status_code == 422


def test_one_bad_file_rejects_whole_batch(app_client: TestClient, blob_storage: InMemoryBlobStorage):
    files = _files(("report.pdf", PDF_BYTES), ("photo.png", PNG_BYTES), ("fake.pdf", b"MZ\x90\x00binary"))
    response = app_client.post(URL, headers=_auth(), files=files)

    assert response.status_code == 415
    body = response.json()
    assert "nothing was stored" in body["detail"]
    assert [(e["index"], e["filename"], e["detected_mime"]) for e in body["errors"]] == [
        (1, "photo.png", "image/png"),
        (2, "fake.pdf", "application/x-msdownload"),
    ]
    assert all("too_large" not in e for e in body["errors"])
    assert blob_storage.objects == {}


def test_all_too_large_returns_413(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10)
    response = app_client.post(URL, headers=_auth(), files=_files(("a.pdf", PDF_BYTES), ("b.txt", TXT_BYTES)))
    assert response.status_code == 413
    assert len(response.json()["errors"]) == 2


def test_uploads_each_allowed_type(client: TestClient, blob_storage: InMemoryBlobStorage, business: dict):
    response = client.post(
        URL, headers=business["headers"], files=_files(*[(name, data) for name, data, _ in ONE_OF_EACH])
    )

    assert response.status_code == 201, response.text
    documents = response.json()["documents"]
    business_id = business["business_id"]
    assert [d["filename"] for d in documents] == [name for name, _, _ in ONE_OF_EACH]
    assert [d["detected_mime"] for d in documents] == [mime for _, _, mime in ONE_OF_EACH]
    for doc, (name, data, mime) in zip(documents, ONE_OF_EACH, strict=True):
        assert doc["status"] == "QUEUED"
        assert doc["size_bytes"] == len(data)
        ext = name.rsplit(".", 1)[1]
        assert doc["storage_key"] == f"{business_id}/{doc['doc_id']}/source.{ext}"
        assert blob_storage.objects[doc["storage_key"]] == (data, mime)

    rows = {row.id: row for row in _documents_of(business_id)}
    assert set(rows) == {d["doc_id"] for d in documents}
    assert all(row.status is DocumentStatus.QUEUED for row in rows.values())


def test_accepts_max_files(client: TestClient, blob_storage: InMemoryBlobStorage, business: dict):
    count = get_settings().max_upload_files
    files = _files(*[(f"note-{i}.txt", TXT_BYTES) for i in range(count)])
    response = client.post(URL, headers=business["headers"], files=files)
    assert response.status_code == 201
    assert len(response.json()["documents"]) == count
    assert len(blob_storage.objects) == count


def test_business_comes_from_token_and_filename_is_sanitized(
    client: TestClient, blob_storage: InMemoryBlobStorage, business: dict
):
    response = client.post(
        URL,
        headers=business["headers"],
        data={"business_id": "biz_someoneelse"},
        files=_files(("..\\..\\etc/passwd.txt", TXT_BYTES)),
    )
    assert response.status_code == 201
    doc = response.json()["documents"][0]
    assert doc["filename"] == "passwd.txt"
    assert doc["storage_key"].startswith(f"{business['business_id']}/")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("bad\x00\x1fname\x7f.txt", "badname.txt"),
        ("  spaced.txt  ", "spaced.txt"),
        ("", "untitled"),
        (None, "untitled"),
        ("dir/", "untitled"),
        ("a" * 300 + ".txt", "a" * 255),
    ],
)
def test_clean_filename(raw: str | None, expected: str):
    assert _clean_filename(raw) == expected


def test_storage_failure_stores_nothing(client: TestClient, blob_storage: InMemoryBlobStorage, business: dict):
    blob_storage.fail_on_put = "source.docx"
    files = _files(("report.pdf", PDF_BYTES), ("letter.docx", DOCX_BYTES), ("notes.txt", TXT_BYTES))
    response = client.post(URL, headers=business["headers"], files=files)

    assert response.status_code == 502
    assert blob_storage.objects == {}
    assert _documents_of(business["business_id"]) == []


def test_database_failure_removes_stored_objects(migrated_db: None, blob_storage: InMemoryBlobStorage, client):
    # A token for a business that doesn't exist violates the documents → businesses foreign key.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.post(URL, headers=_auth("biz_doesnotexist"), files=_files(("a.txt", TXT_BYTES)))
    assert response.status_code == 500
    assert blob_storage.objects == {}
