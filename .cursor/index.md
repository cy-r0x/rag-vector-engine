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
| `api/main.py` | FastAPI app: logging setup, JWT middleware, app-wide `require_auth`, router, `GET /health`; lifespan runs the document events consumer (`CONSUME_DOCUMENT_EVENTS`) | done |
| `api/consumers/document_events.py` | Consumes `DOCUMENT_EVENTS_QUEUE`; `apply_status_event` updates `status` + `status_detail` (failure reason) on the row matching `doc_id` + `business_id`; retries the first connection so the API starts without RabbitMQ | done |
| `api/routes/routes.py` | Single router (`/v1`); maps paths to handlers via `add_api_route` | done |
| `api/handlers/auth.py` | `register` (creates business + owner user, returns JWT; 409 on duplicate email) / `login` (401 on bad credentials) | done |
| `api/handlers/documents.py` | `GET /v1/documents`: the caller's documents, newest first. `GET /v1/documents/{doc_id}` and `GET /v1/documents/{doc_id}/markdown` (extracted Markdown, 409 unless INDEXED; 404 for other tenants). `POST /v1/documents/upload` (publishes one `IngestionJob` per document after the batch is stored; 503 + batch discarded if RabbitMQ doesn't confirm): ≤50 files (`files` field), PDF/DOCX/XLSX/CSV/TXT, all-or-nothing; stores in blob storage + `documents` row (`QUEUED`). No queue publish yet | done |
| `api/middleware/auth.py` | `JWTAuthMiddleware` (decodes Bearer token → `request.state.token_payload`), `require_auth`, `get_token_payload`, `get_business_id` (claim → `UUID`, 401 if malformed) | done |
| `api/middleware/request_logger.py` | One log line per request (method, path, status, duration) | done |
| `api/middleware/rate_limit.py` | Redis token bucket | stub |
| `api/validators/file_type.py` | `detect_file_type`: magic-byte upload validation (`filetype` + own ZIP/CSV/TXT checks); `allowed=` narrows the allowlist per route | done |
| `common/config/` | `Settings` / `get_settings()` (pydantic-settings, reads `.env`) | done |
| `common/logger/` | `setup_logging()` (format, `LOG_LEVEL`, disables uvicorn access log) | done |
| `common/auth/` | `JWTCodec` (HS256 encode/decode), `get_jwt_codec()`; `PasswordHasher` (Argon2id), `get_password_hasher()` | done |
| `common/decorators/` | `@public`, `is_public()` | done |
| `db/postgres.py` | Async engine, `get_session` (FastAPI dependency), `dispose_engine` (app shutdown) | done |
| `db/models/schema.py` | `Base`, `Business`, `User` (unique lowercased email, role), `Document` (`DocumentStatus`, nullable `status_detail` failure reason); all primary keys are native UUIDv7 | done |
| `db/models/events.py` | `IngestionJob` (JSON payload published per uploaded document; `from_document()`), `DocumentStatusEvent` (worker → API status update) | done |
| `db/migrations/`, `alembic.ini` | Alembic (async); `env.py` uses `DATABASE_URL` unless `sqlalchemy.url` is set | done |
| `infra/queue/` | RabbitMQ `connect` / `open_channel` / `declare_queue`; `JobPublisher` / `RabbitJobPublisher` (confirmed, mandatory, persistent publishes; plain connection opened on demand), `get_job_publisher()` (FastAPI dependency, closed on shutdown) | done |
| `infra/redis/` | Redis client | stub |
| `infra/storage/blob.py` | `BlobStorage` + `S3BlobStorage` (MinIO now, AWS S3 via env), `object_key()` (source), `extracted_markdown_key()`, `chunks_key()`, `document_prefix()` | done |
| `infra/storage/vector_db.py` | Qdrant client with `business_id` pre-filter | stub |
| `workers/consumer.py` | Consumes `INGESTION_QUEUE`: reports `PROCESSING`, fetches the source, extracts, stores `extracted.md`, chunks it into `chunks.jsonl`, reports `INDEXED` (placeholder) or `FAILED` + detail; malformed jobs rejected; infra errors requeue (no DLQ yet) | done |
| `workers/extractor.py` | `extract_markdown(data, mime)`: PDF (pypdf), DOCX (python-docx), XLSX (openpyxl), CSV, TXT → Markdown; `ExtractionError` / `NeedsOcrError` | done |
| `workers/publisher.py` | `publish_status()`: confirmed, persistent `DocumentStatusEvent` to `DOCUMENT_EVENTS_QUEUE` | done |
| `workers/chunker.py` | `chunk_markdown(markdown, size, overlap)`: simple paragraph packer (`CHUNK_SIZE` / `CHUNK_OVERLAP`), `to_jsonl()`; not header/table-aware yet | done |
| `workers/{ocr,embedder}.py` | Ingestion pipeline steps | stub |
| `tests/` | Mirrors the source tree; MinIO/Postgres/RabbitMQ tests skip if the service is down (Postgres tests use the `rve_test` DB via the `migrated_db`/`client` fixtures in `tests/api/conftest.py`; `blob_storage` fixture swaps in an in-memory store) | done |

## Conventions

- Python, snake_case. Tenant identifier is `business_id` everywhere.
- IDs are UUIDv7 (`uuid.uuid7()`, generated by the app) stored as native Postgres `uuid`; JWT claims carry them as strings, so read the tenant with the `get_business_id` dependency.
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
