import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware.auth import JWTAuthMiddleware
from api.middleware.request_logger import RequestLoggingMiddleware
from core.tokens import JWTCodec

CODEC = JWTCodec(secret="unit-test-secret-that-is-at-least-32-chars")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(JWTAuthMiddleware, codec=CODEC, public_paths={"/health", "/boom"})
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/me")
    async def me():
        return {}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=False)


def _request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "api.request"]


def test_successful_request_is_logged_at_info(client: TestClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO):
        client.get("/health")
    [record] = _request_records(caplog)
    assert record.levelno == logging.INFO
    assert "GET /health -> 200" in record.getMessage()
    assert "business_id=-" in record.getMessage()


def test_authenticated_request_logs_business_id(client: TestClient, caplog: pytest.LogCaptureFixture):
    token = CODEC.encode(sub="user_1", business_id="biz_42")
    with caplog.at_level(logging.INFO):
        client.get("/me", headers={"Authorization": f"Bearer {token}"})
    [record] = _request_records(caplog)
    assert "GET /me -> 200" in record.getMessage()
    assert "business_id=biz_42" in record.getMessage()


def test_rejected_request_logs_reason_and_401(client: TestClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO):
        client.get("/me")
    [request_record] = _request_records(caplog)
    assert request_record.levelno == logging.WARNING
    assert "GET /me -> 401" in request_record.getMessage()

    [auth_record] = [r for r in caplog.records if r.name == "api.auth"]
    assert "Missing or malformed Authorization header" in auth_record.getMessage()


def test_unhandled_exception_is_logged_as_500(client: TestClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO):
        response = client.get("/boom")
    assert response.status_code == 500
    [record] = _request_records(caplog)
    assert record.levelno == logging.ERROR
    assert "GET /boom -> 500" in record.getMessage()
    assert record.exc_info is not None


def test_query_string_is_not_logged(client: TestClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO):
        client.get("/health?token=secret-value")
    [record] = _request_records(caplog)
    assert "secret-value" not in record.getMessage()
