# Lessons

Things that went wrong or behaved unexpectedly, and what we do about them.

## RabbitMQ / aio-pika

- **Don't publish from request handlers over a robust connection.** `connect_robust` reconnects on a fixed 5 s
  timer and reports "not closed" while reconnecting, so a publish in that window fails with "Connection was not
  opened". `RabbitJobPublisher` therefore uses a plain `aio_pika.connect` opened on demand and drops it after any
  failure: fails fast while RabbitMQ is down, reconnects on the next upload. Long-lived consumers keep using the
  robust connection.
- **Unroutable messages vanish silently** unless the channel uses `on_return_raises=True` and publishes are
  `mandatory=True` (with publisher confirms).
- **Consumers handle messages concurrently**, so status events can be applied out of order: a late `PROCESSING`
  must not overwrite `INDEXED`/`FAILED` (guarded in `apply_status_event`).
- **The worker has no auto-reload.** After changing worker code, `python -m workers.consumer` must be restarted, or
  it keeps processing jobs with the old code (this caused documents to be acked without a status once).
- `rabbitmq-diagnostics check_port_connectivity` passes before the broker accepts AMQP handshakes; use
  `check_running` before testing reconnects.

## Gemini

- **Models get retired for new keys:** `gemini-2.5-flash` returned 404 "no longer available to new users". List
  models with the key (`client.models.list()`) and probe with a timeout before picking one.
- Heavily loaded models return 503/504; the SDK's `HttpRetryOptions` handles transient codes. Embedding errors carry
  `retryable` so the worker requeues transient failures but fails documents whose input was rejected (4xx).
- Only Gemini's full-size (3072) embedding output is pre-normalized; reduced dimensions must be L2-normalized.

## Qdrant

- Use `is_tenant=True` on the `business_id` keyword index and `HnswConfigDiff(m=0, payload_m=16)` for per-tenant
  graphs; queries without the tenant filter are then not even efficient, which is intended.
- Deterministic point ids (`uuid5(doc_id, chunk_index)`) let re-indexing overwrite in place; delete only chunks with
  `chunk_index >= len(new_chunks)` afterwards so the document never disappears from search.

## FastAPI / Starlette

- App-wide dependencies also run for WebSocket routes, where a `Request` parameter can't be resolved: `require_auth`
  takes an `HTTPConnection`, and WebSocket endpoints are `@public` and authenticate their `token` query param.
- uvicorn logs the full WebSocket URL (including `?token=`) through `uvicorn.error`; `RedactTokensFilter` masks it.
- After `session.commit()` with `expire_on_commit=False`, server-default columns (e.g. `created_at`) are populated
  via RETURNING on Postgres, so `IngestionJob.from_document()` can read them without a lazy load.

## Tooling

- The project uses a plain `.venv` with pip (Python 3.14); install with `pip install -r requirements-dev.txt`.
- The API auto-reloads on file changes (StatReload); a model/migration mismatch shows up as a transient
  `UndefinedColumn` if the code reloads before `alembic upgrade head` runs.
