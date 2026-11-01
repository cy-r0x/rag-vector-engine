import asyncio
import os
import socket
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import BinaryIO

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.main import app
from db import get_session
from infra.storage.blob import ObjectInfo, ObjectNotFoundError, get_blob_storage

DATABASE_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://rve:rve@localhost:5433/rve_test")
_MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _postgres_reachable() -> bool:
    url = make_url(DATABASE_URL)
    try:
        with socket.create_connection((url.host, url.port or 5432), timeout=2):
            return True
    except OSError:
        return False


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


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[None]:
    """Migrated test database; tests that depend on it are skipped when Postgres is down."""
    if not _postgres_reachable():
        pytest.skip(f"Postgres not reachable at {DATABASE_URL}")
    asyncio.run(_create_database_if_missing())
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


def run_db(fn):
    """Run `await fn(session)` against the test database from a sync test."""

    async def runner():
        engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


@pytest.fixture
def client(migrated_db: None) -> Iterator[TestClient]:
    """App client backed by the test database."""
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


class InMemoryBlobStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail_on_put: str | None = None

    async def ensure_bucket(self) -> None:
        pass

    async def put_object(
        self, key: str, body: bytes | BinaryIO, content_type: str, metadata: Mapping[str, str] | None = None
    ) -> None:
        if self.fail_on_put is not None and key.endswith(self.fail_on_put):
            raise ConnectionError("storage unavailable")
        self.objects[key] = (body if isinstance(body, bytes) else body.read(), content_type)

    async def get_object(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key][0]

    async def head_object(self, key: str) -> ObjectInfo:
        data, content_type = self.objects[key]
        return ObjectInfo(key=key, size=len(data), content_type=content_type)

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self.objects if key.startswith(prefix)]
        for key in keys:
            del self.objects[key]
        return len(keys)


@pytest.fixture
def blob_storage() -> Iterator[InMemoryBlobStorage]:
    storage = InMemoryBlobStorage()
    app.dependency_overrides[get_blob_storage] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_blob_storage, None)
