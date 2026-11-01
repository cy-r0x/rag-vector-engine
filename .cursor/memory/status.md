# Status

Last updated: 2026-11-01.

## Works end to end (verified in the browser with Playwright)

1. Register / log in (dashboard login and register pages; JWT in an httpOnly cookie on the frontend).
2. Upload documents (browser checks + server magic-byte validation, all-or-nothing, up to 200 MB per batch via a
   streaming Next.js route handler).
3. Worker: extract (PDF text layer, DOCX, XLSX, CSV, TXT) → chunk → embed (Gemini, 768 dims) → upsert to Qdrant
   under the business → `INDEXED`; failures show their reason in the dashboard.
4. Document list with statuses, Markdown viewer per indexed document.
5. Chat page: per-business WebSocket, retrieval (top 5, ≥ 0.65, tenant pre-filter), streamed grounded answer with
   numbered sources; "couldn't find anything" without calling the model when nothing passes the threshold.
6. Socket access page: socket URL, generate/rotate the static socket token (shown once); a Python `websockets`
   client using the token in the `Authorization` header got a grounded answer, and the old token was refused (4401)
   after rotation.

Backend test suite: 252 tests (`pytest -q`), all passing with Postgres, RabbitMQ, MinIO, Qdrant and a Gemini key.

## Known gaps

- No OCR: scanned PDFs and images fail with "OCR is not available yet" (`workers/ocr.py` is a stub).
- No document delete / re-index endpoints (README sections 4-5); `VectorStore.delete_document` exists but nothing
  calls it, and deleting would also need object cleanup.
- Documents uploaded before embedding was added have no vectors (re-upload or re-index needed).
- No retry limit / dead-letter queue on `ingestion.jobs`.
- No chat history (README: per-tenant, per-session, 7-day retention) and no conversation memory.
- Chunker is not header/table-aware (tables can be split across chunks).
- No dashboard auto-refresh of statuses (Refresh button only); README mentions polling.
- Redis (rate limiting, sessions) not started: `infra/redis/`, `api/middleware/rate_limit.py` are stubs.
- WebSocket has no Origin check (the short-lived ticket in the URL is the protection).
- No rate limiting or usage metering per socket token yet; tokens have no expiry (rotation is the only revocation).

## Likely next steps

- Delete and re-index endpoints (Qdrant + MinIO + Postgres), then a "re-index all" for older documents.
- Chat history table with 7-day retention, and passing recent turns as conversation context.
- Header/table-aware chunking; OCR for scanned PDFs and images.
- DLQ with a retry limit for ingestion jobs.

## Local dev data

- Test business account: `agent-test+1@example.com` (created during testing; several sample documents uploaded).
- The dev Postgres was reset once (UUIDv7 change); Qdrant collection `document_chunks` holds only documents
  indexed after embedding was added.
