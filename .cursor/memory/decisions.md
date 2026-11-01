# Decisions

Decisions made with the user, newest last. Each says what was chosen and why, so later work doesn't re-open them
by accident. If a request contradicts one of these, ask first (see `rules/ask-when-unclear.mdc`).

## Identity and data

- **IDs are native Postgres UUIDv7, no prefixes** (`biz_…`/`usr_…`/`doc_…` were dropped). Generated in the app with
  Python 3.14's `uuid.uuid7()` because Postgres 17 has no `uuidv7()`. Time-ordered, so `ORDER BY id DESC` is
  "newest first". At the time the initial migrations `0001`/`0002` were rewritten in place (dev DB reset); since
  then migrations are committed, so schema changes get a new revision.
- **Failure reason is stored** as `documents.status_detail` (migration `0003`) and shown in the dashboard; it is
  cleared by any later non-FAILED status.

## Ingestion pipeline

- **Upload flow:** store every file in object storage first, commit the rows, then publish **one JSON job per
  document** to `ingestion.jobs` once the whole batch is stored. The worker fetches the file via `storage_key`.
- **Upload stays all-or-nothing:** if RabbitMQ doesn't confirm every job, the batch's rows and objects are deleted
  and the API returns 503 (no transactional outbox for now).
- **Retries/DLQ deferred:** only publishing was built; the worker requeues on infrastructure errors with a 5 s
  pause but there is no retry limit or dead-letter queue yet.
- **Status updates go through `document.events`** (README design): the worker publishes `DocumentStatusEvent`s,
  the API consumes them in a background task and updates Postgres. "Completed" maps to the existing `INDEXED`.
- **Extraction (non-OCR only for now):** pypdf + python-docx + openpyxl + stdlib csv (permissive licences; pymupdf
  rejected for AGPL, markitdown for heavy deps). Output is Markdown at `{business_id}/{doc_id}/extracted.md`.
  Scanned PDFs (no text layer) and images are reported `FAILED` ("OCR is not available yet").
- **Simple chunker for now:** paragraph packing, 1000 chars, 150 overlap (configurable), stored as
  `chunks.jsonl`. Header/table-aware chunking (README design) comes later.

## Embeddings, vectors, chat

- **Gemini embeddings behind a provider pattern** (`infra/embeddings`): `gemini-embedding-001` at **768
  dimensions**, L2-normalized. Swapping providers = new module + registry entry.
- **Vector DB is Qdrant** (over pgvector/Weaviate/Milvus/Pinecone) because its filterable HNSW applies the
  mandatory `business_id` pre-filter inside the search, with `is_tenant` indexing and per-tenant graphs. One
  collection for all tenants.
- **Chat is a per-business WebSocket** `/v1/chat/ws/{business_id}` doing full RAG with streamed Gemini answers
  (same provider pattern, `infra/llm`). The path is only trusted after it matches the token's business.
- **Retrieval: top-k = 5 with minimum similarity 0.65.** The user first asked for 0.75, but the correct match in a
  live test scored 0.729, so 0.65 was chosen (configurable: `RETRIEVAL_TOP_K`, `RETRIEVAL_MIN_SCORE`).
- **Chat model default is `gemini-3.5-flash-lite`.** `gemini-2.5-flash` (the original choice) is retired for new
  API keys; Google's suggested `gemini-3.8-flash` was timing out when tested. Switch with `LLM_MODEL`.
- **Browsers use short-lived chat tickets** (`POST /v1/chat/ticket`, 60 s, chat-only) so the access token stays in
  the frontend's httpOnly cookie.
- **Chat history (7-day retention) deferred;** each question is answered independently (no conversation memory).
