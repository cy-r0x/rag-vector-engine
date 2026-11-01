# Project index

Multi-tenant RAG-as-a-service backend (FastAPI + RabbitMQ worker). Full design: [README.md](../README.md). The frontend lives in a separate repo.

## `.cursor/` contents

- [rules/ask-when-unclear.mdc](rules/ask-when-unclear.mdc) — always-on rule: ask the user instead of guessing when a request is ambiguous.
- [memory/](memory/) — project memory notes (empty so far).
- `index.md` — this file.

## Code map

Status: **done** = implemented and tested, **stub** = empty file reserved by the README layout.

| Path | What it is | Status |
|------|------------|--------|
| `api/main.py` | FastAPI app: logging setup, JWT middleware, app-wide `require_auth`, router, `GET /health` | done |
| `api/routes/routes.py` | Single router (`/v1`); maps paths to handlers via `add_api_route` | done |
| `api/handlers/auth.py` | `register` (creates business + owner user, returns JWT; 409 on duplicate email) / `login` (401 on bad credentials) | done |
| `api/middleware/auth.py` | `JWTAuthMiddleware` (decodes Bearer token → `request.state.token_payload`), `require_auth`, `get_token_payload` | done |
| `api/middleware/request_logger.py` | One log line per request (method, path, status, duration) | done |
| `api/middleware/rate_limit.py` | Redis token bucket | stub |
| `api/validators/file_type.py` | `detect_file_type`: magic-byte upload validation (`filetype` + own ZIP/CSV checks) | done |
| `common/config/` | `Settings` / `get_settings()` (pydantic-settings, reads `.env`) | done |
| `common/logger/` | `setup_logging()` (format, `LOG_LEVEL`, disables uvicorn access log) | done |
| `common/auth/` | `JWTCodec` (HS256 encode/decode), `get_jwt_codec()`; `PasswordHasher` (Argon2id), `get_password_hasher()` | done |
| `common/decorators/` | `@public`, `is_public()` | done |
| `db/postgres.py` | Async engine, `get_session` (FastAPI dependency), `dispose_engine` (app shutdown) | done |
| `db/models/schema.py` | `Base`, `Business` (`biz_…`), `User` (`usr_…`, unique lowercased email, role) | done |
| `db/models/events.py` | RabbitMQ job & status payloads | stub |
| `db/migrations/`, `alembic.ini` | Alembic (async); `env.py` uses `DATABASE_URL` unless `sqlalchemy.url` is set | done |
| `infra/queue/` | RabbitMQ `connect` / `open_channel` / `declare_queue` | done |
| `infra/redis/` | Redis client | stub |
| `infra/storage/blob.py` | `BlobStorage` + `S3BlobStorage` (MinIO now, AWS S3 via env), `object_key()` | done |
| `infra/storage/vector_db.py` | Qdrant client with `business_id` pre-filter | stub |
| `workers/consumer.py` | Consumes `INGESTION_QUEUE`; logs + acks (no DLQ yet) | done |
| `workers/{extractor,ocr,chunker,embedder,publisher}.py` | Ingestion pipeline steps | stub |
| `tests/` | Mirrors the source tree; MinIO/Postgres/RabbitMQ tests skip if the service is down (Postgres tests use the `rve_test` DB) | done |

## Conventions

- Python, snake_case. Tenant identifier is `business_id` everywhere (IDs prefixed `biz_`).
- Routes stay thin: `api/routes/routes.py` registers handlers from `api/handlers/`.
- Every route requires a JWT unless its endpoint is decorated with `@public` (`from common.decorators import public`).
- Shared code is imported from package roots: `common.config`, `common.logger`, `common.auth`, `common.decorators`, `infra.queue`.
- Settings come only from `common.config.get_settings()`; document new variables in `.env.example`.
- Queues must be declared via `infra.queue.declare_queue` so publisher and consumer use identical arguments.
- Schema changes go through Alembic: edit `db/models/schema.py`, then `alembic revision --autogenerate -m "..."`.
- `business_id` in handlers comes from the token (`get_token_payload`), never from the request body.

## Run & test

```powershell
docker compose up -d                      # MinIO (+ bucket init), Postgres (host port 5433), RabbitMQ
.venv\Scripts\activate
pip install -r requirements-dev.txt
alembic upgrade head                      # apply DB migrations
python -m api.main                        # API on http://127.0.0.1:8000 (auto-reload)
python -m workers.consumer                # ingestion worker
pytest -q
```

Local consoles: MinIO http://localhost:9001 (`minioadmin`/`minioadmin`), RabbitMQ http://localhost:15672 (`rve`/`rve`).
