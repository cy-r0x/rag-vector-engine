import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import pool, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.main import app
from common.auth import get_jwt_codec
from db import get_session
from db.models import User

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://rve:rve@localhost:5433/rve_test")
_MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _postgres_reachable() -> bool:
    url = make_url(DATABASE_URL)
    try:
        with socket.create_connection((url.host, url.port or 5432), timeout=2):
            return True
    except OSError:
        return False


requires_postgres = pytest.mark.skipif(not _postgres_reachable(), reason=f"Postgres not reachable at {DATABASE_URL}")


async def _create_database_if_missing() -> None:
    url = make_url(DATABASE_URL)
    conn = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, database="postgres"
    )
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database):
            await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def migrated_db() -> Iterator[None]:
    asyncio.run(_create_database_if_missing())
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


@pytest.fixture
def client(migrated_db: None) -> Iterator[TestClient]:
    # NullPool: TestClient runs the app on its own event loop, so connections must not be pooled across loops.
    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(engine.dispose())


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


@requires_postgres
def test_register_creates_business_and_returns_token(client: TestClient):
    data = _register(client, _email())

    assert data["business_id"].startswith("biz_")
    assert data["user_id"].startswith("usr_")
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == get_jwt_codec().expires_in
    claims = get_jwt_codec().decode(data["access_token"])
    assert claims["sub"] == data["user_id"]
    assert claims["business_id"] == data["business_id"]


@requires_postgres
def test_register_stores_owner_with_hashed_password(client: TestClient):
    email = _email()
    data = _register(client, email, password="s3cret-passw0rd")

    async def fetch() -> User:
        engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                return await session.scalar(select(User).where(User.id == data["user_id"]))
        finally:
            await engine.dispose()

    user = asyncio.run(fetch())
    assert user.email == email
    assert user.business_id == data["business_id"]
    assert user.role == "owner"
    assert user.password_hash.startswith("$argon2id$")
    assert "s3cret-passw0rd" not in user.password_hash


@requires_postgres
def test_register_rejects_duplicate_email_case_insensitively(client: TestClient):
    email = _email()
    _register(client, email)

    response = client.post(
        "/v1/auth/register",
        json={"business_name": "Other Corp", "email": email.upper(), "password": "s3cret-passw0rd"},
    )
    assert response.status_code == 409


@requires_postgres
def test_login_returns_token_for_same_business(client: TestClient):
    email = _email()
    registered = _register(client, email)

    response = client.post("/v1/auth/login", json={"email": email.upper(), "password": "s3cret-passw0rd"})
    assert response.status_code == 200
    data = response.json()
    assert data["business_id"] == registered["business_id"]
    assert data["user_id"] == registered["user_id"]
    assert get_jwt_codec().decode(data["access_token"])["business_id"] == registered["business_id"]


@requires_postgres
def test_login_rejects_wrong_password_and_unknown_email_identically(client: TestClient):
    email = _email()
    _register(client, email)

    wrong_password = client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})
    unknown_email = client.post("/v1/auth/login", json={"email": _email(), "password": "s3cret-passw0rd"})

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
