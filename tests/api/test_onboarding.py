import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import app
from common.auth import get_jwt_codec
from db.models import User
from tests.api.conftest import run_db


def _email() -> str:
    return f"owner-{uuid.uuid4().hex[:12]}@acme.com"


def _register(client: TestClient, email: str, password: str = "s3cret-passw0rd") -> dict:
    response = client.post(
        "/v1/auth/register", json={"business_name": "Acme Corp", "email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "body",
    [
        {"business_name": "Acme", "email": "a@acme.com", "password": "short"},
        {"business_name": "   ", "email": "a@acme.com", "password": "s3cret-passw0rd"},
        {"business_name": "Acme", "email": "not-an-email", "password": "s3cret-passw0rd"},
        {"business_name": "Acme", "email": "a@acme.com", "password": "x" * 129},
        {"email": "a@acme.com", "password": "s3cret-passw0rd"},
    ],
)
def test_register_rejects_invalid_body(body: dict):
    with TestClient(app) as test_client:
        assert test_client.post("/v1/auth/register", json=body).status_code == 422


def test_register_creates_business_and_returns_token(client: TestClient):
    data = _register(client, _email())

    assert uuid.UUID(data["business_id"]).version == 7
    assert uuid.UUID(data["user_id"]).version == 7
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == get_jwt_codec().expires_in
    claims = get_jwt_codec().decode(data["access_token"])
    assert claims["sub"] == data["user_id"]
    assert claims["business_id"] == data["business_id"]


def test_register_stores_owner_with_hashed_password(client: TestClient):
    email = _email()
    data = _register(client, email, password="s3cret-passw0rd")

    user = run_db(lambda session: session.scalar(select(User).where(User.id == uuid.UUID(data["user_id"]))))
    assert user.email == email
    assert str(user.business_id) == data["business_id"]
    assert user.role == "owner"
    assert user.password_hash.startswith("$argon2id$")
    assert "s3cret-passw0rd" not in user.password_hash


def test_register_rejects_duplicate_email_case_insensitively(client: TestClient):
    email = _email()
    _register(client, email)

    response = client.post(
        "/v1/auth/register",
        json={"business_name": "Other Corp", "email": email.upper(), "password": "s3cret-passw0rd"},
    )
    assert response.status_code == 409


def test_login_returns_token_for_same_business(client: TestClient):
    email = _email()
    registered = _register(client, email)

    response = client.post("/v1/auth/login", json={"email": email.upper(), "password": "s3cret-passw0rd"})
    assert response.status_code == 200
    data = response.json()
    assert data["business_id"] == registered["business_id"]
    assert data["user_id"] == registered["user_id"]
    assert get_jwt_codec().decode(data["access_token"])["business_id"] == registered["business_id"]


def test_login_rejects_wrong_password_and_unknown_email_identically(client: TestClient):
    email = _email()
    _register(client, email)

    wrong_password = client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})
    unknown_email = client.post("/v1/auth/login", json={"email": _email(), "password": "s3cret-passw0rd"})

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
