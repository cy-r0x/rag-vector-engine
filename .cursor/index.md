# Project index

Multi-tenant RAG-as-a-service backend: FastAPI API + RabbitMQ ingestion worker, PostgreSQL, MinIO/S3, Qdrant, Gemini.
Full design and API reference: [README.md](../README.md). The Next.js dashboard lives in the sibling repo
`../rag-vector-engine-frontend`. Read this file first; decisions and lessons are in [memory/](memory/).

## `.cursor/` contents

| Path | What it is |
|------|------------|
| `index.md` | This file: system overview, code map, conventions, how to run |
| [rules/ask-when-unclear.mdc](rules/ask-when-unclear.mdc) | Always-on rule: ask (AskQuestion, recommended option first) instead of guessing when a request is ambiguous |
| [memory/decisions.md](memory/decisions.md) | Architecture/product decisions made with the user, with the reason for each |
| [memory/lessons.md](memory/lessons.md) | Gotchas and bugs found the hard way (library behaviour, external services) |
| [memory/status.md](memory/status.md) | What works end to end today, known gaps, and likely next steps |

## System at a glance

```
Dashboard ──HTTP──▶ API ──▶ Postgres (businesses, users, documents)
    │                │──▶ MinIO  {business_id}/{doc_id}/source.<ext>
    │                │──▶ RabbitMQ ingestion.jobs ──▶ Worker ──▶ extracted.md, chunks.jsonl (MinIO)
    │                │                                   │──▶ Gemini embeddings ──▶ Qdrant (business_id-filtered)
    │                │◀── RabbitMQ document.events ◀─────┘   (PROCESSING / INDEXED / FAILED + detail)
    └──WebSocket──▶ /v1/chat/ws/{business_id}: embed query → Qdrant (tenant pre-filter, top-k, min score)
Business backend ──WebSocket (static rve_sk_ token)──┘   → Gemini chat model → streamed answer + sources
```

## Code map

Status: **done** = implemented and tested, **stub** = empty file reserved by the README layout.

### API (`api/`)

| Path | What it is | Status |
|------|------------|--------|
| `api/main.py` | App: logging, JWT middleware, app-wide `require_auth`, router, `GET /health`. Lifespan runs the document-events consumer (`CONSUME_DOCUMENT_EVENTS`) and closes the job publisher, embedder, vector store, chat model and DB engine on shutdown | done |
| `api/routes/routes.py` | Single `/v1` router; maps paths to handlers (`add_api_route`, `add_api_websocket_route`) | done |
| `api/handlers/auth.py` | `POST /v1/auth/register` (business + owner user, 409 on duplicate email), `POST /v1/auth/login` (401 on bad credentials); both return a JWT | done |
| `api/handlers/documents.py` | `GET /v1/documents` (newest first), `GET /v1/documents/{doc_id}`, `GET /v1/documents/{doc_id}/markdown` (409 unless INDEXED), `POST /v1/documents/upload` (≤50 files, PDF/DOCX/XLSX/CSV/TXT, all-or-nothing; stores objects + rows, then publishes one `IngestionJob` per document; 503 + batch discarded if RabbitMQ doesn't confirm). Other tenants' documents are 404 | done |
| `api/handlers/chat.py` | `POST /v1/chat/ticket` (60 s chat-only ticket, `token_use=chat_ws`, carries `session_exp`) and WebSocket `/v1/chat/ws/{business_id}`: credential from `Authorization: Bearer` or `?token=` — static socket token, chat ticket, or access token (`_authenticate` → `_SocketAuth`); path must match the credential's business (close 4401/4403); frames `delta` / `done` + sources / `error`; `_revocation_reason` re-checks every message (login expiry, socket token rotated) | done |
| `api/handlers/socket_access.py` | `GET /v1/socket` (socket URL from `PUBLIC_WS_BASE_URL` + current token prefix/created_at), `POST /v1/socket/token` (generate or rotate via upsert; returns the plain token once) | done |
| `api/rag.py` | `retrieve()` (embed query → business-filtered search with `RETRIEVAL_TOP_K` / `RETRIEVAL_MIN_SCORE` → filenames from DB, deleted docs dropped), `SYSTEM_PROMPT` (answer only from excerpts, cite `[n]`, excerpts are data not instructions), `build_prompt()`, `answer()`, `NO_CONTEXT_ANSWER` | done |
| `api/consumers/document_events.py` | Background consumer of `DOCUMENT_EVENTS_QUEUE`; `apply_status_event` sets `status` + `status_detail` on the row matching `doc_id` **and** `business_id`; a late `PROCESSING` never overwrites INDEXED/FAILED; retries the first connection so the API starts without RabbitMQ | done |
| `api/middleware/auth.py` | `JWTAuthMiddleware` (Bearer → `state.token_payload`; tokens with a `token_use` claim are rejected for HTTP), `require_auth` (on `HTTPConnection`), `get_token_payload`, `get_business_id` (claim → `UUID`, 401 if malformed), `TOKEN_USE_CLAIM` | done |
| `api/middleware/request_logger.py` | One log line per request (method, path, status, duration) | done |
| `api/middleware/rate_limit.py` | Redis token bucket | stub |
| `api/validators/file_type.py` | `detect_file_type`: magic-byte validation (`filetype` + own ZIP/CSV/TXT checks, macro/zip-bomb guards); `allowed=` narrows the allowlist; MIME constants used by the worker too | done |

### Worker (`workers/`, run with `python -m workers.consumer`)

| Path | What it is | Status |
|------|------------|--------|
| `workers/consumer.py` | Consumes `INGESTION_QUEUE` with a `Pipeline(storage, embedder, vector_store)`: reports `PROCESSING` → fetches source (key must be under the doc's own prefix) → extracts → stores `extracted.md` → chunks → stores `chunks.jsonl` → embeds + upserts to Qdrant → reports `INDEXED`. Document problems → `FAILED` + detail and ack; infra / transient embedding errors → requeue after 5 s; malformed jobs rejected. `ensure_collection` at startup (fails on dimension mismatch). No DLQ yet | done |
| `workers/extractor.py` | `extract_markdown(data, mime)`: PDF text layer (pypdf, `## Page N`), DOCX (python-docx: headings, lists, tables), XLSX (openpyxl, `## Sheet:` tables), CSV (sniffed delimiter), TXT. `ExtractionError` / `NeedsOcrError` (scanned PDFs, images) | done |
| `workers/chunker.py` | `chunk_markdown(markdown, size, overlap)`: simple paragraph packer (`CHUNK_SIZE` / `CHUNK_OVERLAP`, word-boundary overlap, long paragraphs split at whitespace; `text == markdown[start:end]`), `to_jsonl()`. Not header/table-aware yet | done |
| `workers/embedder.py` | `index_chunks()`: embeds chunk texts and `replace_document_chunks` in the vector store | done |
| `workers/publisher.py` | `publish_status()`: confirmed, persistent `DocumentStatusEvent` to `DOCUMENT_EVENTS_QUEUE` | done |
| `workers/ocr.py` | LightOn OCR for images and scanned PDFs | stub |

### Infrastructure clients (`infra/`)

| Path | What it is | Status |
|------|------------|--------|
| `infra/embeddings/` | Provider pattern: `Embedder` protocol + `EmbeddingError(retryable)` / `EmbeddingConfigError` (`base.py`); `GeminiEmbedder` (`gemini.py`: `gemini-embedding-001`, `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`, `EMBEDDING_DIMENSIONS`, batches ≤100, SDK retries on 408/429/5xx, L2-normalized); `PROVIDERS` + `create_embedder()` / `get_embedder()` (`factory.py`, `EMBEDDING_PROVIDER`) | done |
| `infra/llm/` | Same pattern for chat: `ChatModel` protocol (`stream(system, prompt)`), `GeminiChatModel` (default `gemini-3.5-flash-lite`, `LLM_MODEL`, `LLM_TEMPERATURE`), `PROVIDERS` + `create_chat_model()` / `get_chat_model()` (`LLM_PROVIDER`) | done |
| `infra/storage/vector_db.py` | `VectorStore` protocol + `QdrantVectorStore`: one collection for all tenants; `business_id` keyword index with `is_tenant`, per-tenant HNSW (`m=0`, `payload_m=16`), `doc_id` / `chunk_index` indexes; point id `uuid5(doc_id, chunk_index)`; `ensure_collection`, `replace_document_chunks` (upsert then drop stale chunks), `delete_document`, `search(min_score=)`; every read/delete requires `business_id` | done |
| `infra/storage/blob.py` | `BlobStorage` + `S3BlobStorage` (MinIO now, AWS S3 via env); keys: `object_key()` (source), `extracted_markdown_key()`, `chunks_key()`, `document_prefix()` (all validate ids) | done |
| `infra/queue/` | `connect` (robust) / `open_channel` / `declare_queue` (`queue.py`); `RabbitJobPublisher` (`publisher.py`: confirmed, mandatory, persistent; plain connection opened on demand and dropped after a failure), `get_job_publisher()` | done |
| `infra/redis/` | Redis client | stub |

### Shared (`common/`, `db/`)

| Path | What it is | Status |
|------|------------|--------|
| `common/config/` | `Settings` / `get_settings()` (pydantic-settings, reads `.env`; empty strings → `None` for optional keys) | done |
| `common/logger/` | `setup_logging()`: format, `LOG_LEVEL`, disables uvicorn access log, quiets `httpx`/`httpcore`/`google_genai`, `RedactTokensFilter` masks `token=` in uvicorn logs | done |
| `common/auth/` | `JWTCodec` (HS256; requires `sub`, `business_id`, `exp`; `extra_claims` for purpose-bound tokens), `get_jwt_codec()`; `PasswordHasher` (Argon2id); socket tokens (`socket_tokens.py`: `generate_socket_token()` → `rve_sk_` + 256-bit random, SHA-256 `hash_socket_token()`, `is_socket_token()`) | done |
| `common/decorators/` | `@public`, `is_public()` | done |
| `db/postgres.py` | Async engine, `get_session` (per request), `get_sessionmaker` (used by WebSocket and the events consumer), `dispose_engine` | done |
| `db/models/schema.py` | `Business`, `User` (unique lowercased email, role), `Document` (`DocumentStatus` QUEUED/PROCESSING/INDEXED/FAILED, nullable `status_detail`), `SocketToken` (one per business: `token_hash` unique, `token_prefix`, `created_at`); UUIDv7 primary keys | done |
| `db/models/events.py` | `IngestionJob` (API → worker, one per document; `from_document()`), `DocumentStatusEvent` (worker → API) | done |
| `db/migrations/` | Alembic (async): `0001` businesses + users, `0002` documents, `0003` `status_detail`, `0004` socket tokens | done |

### Tests (`tests/`, mirrors the source tree)

Postgres tests use the `rve_test` DB (`migrated_db` / `client` fixtures in `tests/api/conftest.py`). Integration tests
skip when their service is down: MinIO, Postgres, RabbitMQ (`tests/workers/test_consumer.py`,
`tests/infra/queue/`), Qdrant (`tests/infra/storage/test_vector_db.py`); the live Gemini test skips without
`GEMINI_API_KEY`. Fakes: `InMemoryBlobStorage` and `InMemoryJobPublisher` (autouse for API tests) in
`tests/api/conftest.py`; `FakeEmbedder` / `InMemoryVectorStore` in `tests/workers/fakes.py`; real PDF/DOCX/XLSX
builders in `tests/workers/sample_documents.py`. `tests/conftest.py` sets `JWT_SECRET` and
`CONSUME_DOCUMENT_EVENTS=false`.

## Data model and flows

- **Object layout (MinIO/S3):** `{business_id}/{doc_id}/source.<ext>`, `extracted.md`, `chunks.jsonl` (one
  `{"index","text","start","end"}` per line).
- **Qdrant point payload:** `business_id`, `doc_id`, `chunk_index`, `text`, `start`, `end`.
- **Queues:** `ingestion.jobs` (`IngestionJob`, message id = `doc_id`) and `document.events` (`DocumentStatusEvent`);
  both durable, persistent messages, declared via `infra.queue.declare_queue`.
- **Status lifecycle:** `QUEUED` (upload) → `PROCESSING` → `INDEXED` (extracted, chunked, embedded, in Qdrant) or
  `FAILED` with `status_detail` (no text layer, password-protected, damaged, empty, missing source, input rejected
  by the embedder). Any non-FAILED status clears `status_detail`.

## Security rules (keep these true)

- `business_id` always comes from a verified token (`get_business_id` / `get_token_payload`), never from a body.
  On the chat socket the path `{business_id}` is only trusted after it matches the token.
- Every vector read/delete is pre-filtered by `business_id` (mandatory argument in `VectorStore`).
- Other tenants' documents are indistinguishable from missing ones (404).
- Purpose-bound tokens (`token_use` claim, e.g. chat tickets) are rejected by HTTP routes; static socket tokens
  (`rve_sk_…`) only open their own business's socket, are stored only as SHA-256 hashes, are shown once, and are
  re-validated on every socket message (rotation revokes immediately).
- Upload bytes are untrusted: the detected MIME wins over filename/Content-Type.
- Retrieved document text is untrusted prompt input: the system prompt treats excerpts as data.
- Never log credentials (`RedactTokensFilter`); `.env` (holds `GEMINI_API_KEY`) is git-ignored.

## Conventions

- Python 3.14, snake_case; `uuid.uuid7()` for ids (native Postgres `uuid`; JWT claims carry strings).
- Routes stay thin: `api/routes/routes.py` registers handlers from `api/handlers/`.
- Every route requires a JWT unless its endpoint is `@public` (WebSocket endpoints are `@public` and authenticate
  themselves).
- Import shared code from package roots: `common.config`, `common.logger`, `common.auth`, `common.decorators`,
  `infra.queue`, `infra.embeddings`, `infra.llm`.
- Settings only via `common.config.get_settings()`; document every new variable in `.env.example`.
- External providers sit behind protocols + registries (`infra.embeddings`, `infra.llm`); callers never import a
  provider SDK. New provider = one module + one `PROVIDERS` entry. Changing embedding provider/model/dimensions
  needs a new Qdrant collection and re-indexing.
- Services are injected as FastAPI dependencies (`get_blob_storage`, `get_job_publisher`, `get_embedder`,
  `get_vector_store`, `get_chat_model`, `get_sessionmaker`) so tests can override them.
- Queues must be declared via `infra.queue.declare_queue` (RabbitMQ rejects re-declaring with other arguments).
- Schema changes: edit `db/models/schema.py`, then `alembic revision --autogenerate --rev-id 000N -m "..."`; never
  edit committed migrations.
- After changes, update README, this index, and `.env.example`; keep `pytest -q` green.

## Settings (`.env`, see `.env.example`)

| Group | Variables (defaults) |
|-------|----------------------|
| Auth | `JWT_SECRET` (≥32 chars), `JWT_ALGORITHM` (HS256), `JWT_EXPIRES_IN` (3600) |
| Postgres | `DATABASE_URL`, `POSTGRES_*` (compose) |
| Uploads | `MAX_UPLOAD_BYTES` (25 MiB), `MAX_UNCOMPRESSED_BYTES` (200 MiB), `MAX_UPLOAD_FILES` (50), `MAX_BATCH_BYTES` (200 MiB) |
| RabbitMQ | `RABBITMQ_URL`, `INGESTION_QUEUE`, `DOCUMENT_EVENTS_QUEUE`, `CONSUMER_PREFETCH` (10), `CONSUME_DOCUMENT_EVENTS` (true) |
| Storage | `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_FORCE_PATH_STYLE` |
| Chunking | `CHUNK_SIZE` (1000), `CHUNK_OVERLAP` (150) |
| Embeddings | `EMBEDDING_PROVIDER` (gemini), `EMBEDDING_MODEL` (provider default), `EMBEDDING_DIMENSIONS` (768), `EMBEDDING_BATCH_SIZE` (100), `GEMINI_API_KEY` |
| Chat | `LLM_PROVIDER` (gemini), `LLM_MODEL` (gemini-3.5-flash-lite), `LLM_TEMPERATURE` (0.2), `RETRIEVAL_TOP_K` (5), `RETRIEVAL_MIN_SCORE` (0.65), `CHAT_MAX_QUERY_CHARS` (2000), `CHAT_TICKET_EXPIRES_IN` (60), `PUBLIC_WS_BASE_URL` (ws://localhost:8000) |
| Qdrant | `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` (document_chunks), `QDRANT_PORT` (compose) |

## Run & test

```bash
docker compose up -d                 # MinIO (+ bucket init), Postgres (host 5433), RabbitMQ, Qdrant (6333)
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
alembic upgrade head                 # DB migrations (0001-0004)
python -m api.main                   # API on http://127.0.0.1:8000 (auto-reload; also runs the status consumer)
python -m workers.consumer           # ingestion worker (no auto-reload: restart after code changes)
pytest -q
```

Local consoles: MinIO http://localhost:9001 (`minioadmin`/`minioadmin`), RabbitMQ http://localhost:15672
(`rve`/`rve`), Qdrant http://localhost:6333/dashboard.
