# ⚡ rag-vector-engine

> **Enterprise-grade Multi-Tenant RAG-as-a-Service Platform.**  
> Ingests unstructured business files (`.pdf`, `.docx`, `.xlsx`, `.csv`, images), converts them to layout-aware Markdown via **direct extraction** (or **LightOn OCR** for images and scanned PDFs), indexes into tenant-isolated vector partitions, and streams answers over authenticated WebSockets.

---

## 🎯 Architecture Overview

`rag-vector-engine` decouples compute-heavy, asynchronous document ingestion from low-latency, real-time query inference.

```mermaid
flowchart TD
    subgraph Client ["🏢 Onboarded Business"]
        TenantUI["Next.js Dashboard<br/>(Upload & Document Status)"]
        TenantWS["Next.js Chat UI<br/>(WebSocket Client)"]
    end

    subgraph API_Gateway ["🚪 FastAPI Gateway"]
        AuthMiddleware["Tenant Auth & Rate Limiting"]
        DocRouter["/v1/documents<br/>(upload, list, delete, reindex)"]
        FileValidator{"Byte-Level MIME Check<br/>(magic bytes + allowlist)"}
        WSRouter["ws://.../v1/chat/ws/{business_id}"]
    end

    subgraph Storage ["💾 Persistence & Cache"]
        PG[("PostgreSQL<br/>(Metadata, Keys, Jobs,<br/>Chat History: 7-day retention)")]
        MinIO[("MinIO / S3<br/>(Raw Files)")]
        Redis[("Redis<br/>(Rate Limits & Sessions)")]
        VectorDB[("Qdrant<br/>(Indexed business_id Payload)")]
    end

    subgraph Async_Engine ["⚙️ Ingestion & Status Pipeline"]
        JobQueue[["RabbitMQ: ingestion.jobs<br/>(+ DLQ)"]]
        Worker["Async Python Worker"]
        Router{"File type?"}
        Extractor["Direct Extraction<br/>(PDF text layer, DOCX, XLSX, CSV)"]
        OCR["LightOn OCR<br/>(Images & Scanned PDFs)"]
        Chunker["Markdown Chunker + Embedder<br/>(OpenAI or Gemini, TBD)"]
        StatusQueue[["RabbitMQ: document.events"]]
    end

    %% Upload & Ingestion Flow
    TenantUI -->|1. Upload Doc + Bearer Key| DocRouter
    DocRouter -->|Validate Tenant| AuthMiddleware
    AuthMiddleware -.-> PG
    DocRouter -->|2. Sniff File Header| FileValidator
    FileValidator -.->|"Rejected → 415"| TenantUI
    FileValidator -->|"3. Stream Raw File"| MinIO
    FileValidator -->|"4. Publish Job (detected MIME)"| JobQueue
    JobQueue -->|5. Consume Job| Worker
    Worker -->|Fetch File| MinIO
    Worker -->|6. Route by Detected MIME| Router
    Router -->|"Text-based"| Extractor
    Router -->|"Image / No Text Layer"| OCR
    Extractor -->|"Markdown"| Chunker
    OCR -->|"Markdown"| Chunker
    Chunker -->|7. Upsert Vectors| VectorDB
    Worker -->|8a. ACK| JobQueue
    Worker -->|"8b. Failure / Max Retries → DLQ"| JobQueue
    Worker -->|"9. Publish Status: INDEXED / FAILED"| StatusQueue
    StatusQueue -->|10. Consume Status| API_Gateway
    API_Gateway -->|Update Status| PG
    TenantUI -.->|"11. Poll GET /v1/documents"| DocRouter

    %% Query & Chat Flow
    TenantWS <-->|"Connect with ?token= API Key"| WSRouter
    WSRouter -->|"1. Token Check<br/>(key's tenant must match path)"| AuthMiddleware
    WSRouter -->|2. Query Embedding| WSRouter
    WSRouter -->|"3. Prefiltered Vector Search<br/>(business_id == caller)"| VectorDB
    VectorDB -->|Top-K Markdown Chunks| WSRouter
    WSRouter -->|4. Prompt + Context| LLM["OpenAI / LLM Provider"]
    LLM -->|5. Token Stream| WSRouter
    WSRouter -->|"6. WebSocket Frames<br/>(delta / done + sources)"| TenantWS
    WSRouter -->|7. Save Chat Turn| PG
```

---

## ✨ Core Features

* **Multi-Tenant Isolation:** Complete logical tenant separation across relational storage, object storage prefixes (`minio://documents/{business_id}/...`), and vector indexes.
* **Byte-Level File Validation:** Uploaded files are untrusted. The client-supplied `Content-Type` header and file extension are ignored; the real MIME type is detected from the file's magic bytes and checked against an allowlist before anything is stored or queued (see [Upload Validation](#-upload-validation)).
* **Direct Extraction First, OCR Only When Needed:** Text-based files are parsed natively into Markdown (PDF text layer, DOCX headings/tables, XLSX/CSV sheets as Markdown tables), which is faster and more accurate than OCR and needs no GPU. Only images (`.png`, `.jpg`, `.tiff`) and scanned PDFs without a text layer are routed to **LightOn OCR**, which transcribes multi-column layouts, tables, and forms into structured Markdown.
* **Semantic & Markdown-Aware Chunking:** Chunks along `#`, `##`, and `###` headers while keeping tables intact. Metadata paths are prepended to every chunk vector.
* **Pluggable Embeddings:** The embedding provider is not fixed yet; OpenAI or Gemini are the likely candidates. `workers/embedder.py` is the single place the choice is made, and the same model must be used for indexing and for query embedding.
* **Reliable Async Processing:** Ingestion jobs flow through a RabbitMQ work queue with retries and a Dead-Letter Queue (DLQ). Document status changes (`PROCESSING`, `INDEXED`, `FAILED`) are published to a RabbitMQ `document.events` queue and consumed by the API to update PostgreSQL.
* **Status by Polling:** The dashboard polls `GET /v1/documents` to show live document status; there is no push channel for status updates.
* **Deletion & Re-indexing:** Tenants can delete a document (raw file, vectors, and metadata) or re-index it from the stored source file, e.g. after changing the chunker or embedding model.
* **Strict Prefiltered Vector Retrieval:** Queries apply hard pre-filtering on `business_id` before similarity calculation, eliminating cross-tenant data leaks. `business_id` is a payload index in Qdrant so the filter stays fast as tenants grow.
* **Token-Authenticated WebSocket Streaming:** Real-time conversational interface (`/v1/chat/ws/{business_id}`) serving low-latency streamed token frames and source citations.
* **Chat History with 7-Day Retention:** Each chat turn (question, answer, cited sources) is stored in PostgreSQL per tenant and session. Rows older than 7 days are purged by a scheduled cleanup job.

---

## 🗂️ Repository Layout

```text
rag-vector-engine/
├── client/                     # Next.js 16 (App Router) + Tailwind CSS v4 frontend
│   ├── app/
│   │   ├── layout.tsx          # Root layout & global providers
│   │   ├── page.tsx            # Landing / tenant onboarding
│   │   ├── globals.css         # Tailwind entry
│   │   ├── documents/
│   │   │   └── page.tsx        # Upload & live document status
│   │   └── chat/
│   │       └── page.tsx        # Streaming chat with citations
│   ├── components/
│   │   ├── UploadDropzone.tsx  # Drag-and-drop file upload
│   │   ├── DocumentTable.tsx   # Document list with status
│   │   ├── StatusBadge.tsx     # QUEUED / PROCESSING / INDEXED / FAILED
│   │   ├── ChatWindow.tsx      # Message list + input
│   │   ├── MessageBubble.tsx   # Single chat message
│   │   └── SourceCitations.tsx # Source chunks for an answer
│   ├── lib/
│   │   ├── api.ts              # REST client for the FastAPI gateway
│   │   ├── ws.ts               # WebSocket chat client
│   │   └── types.ts            # Shared API types
│   ├── public/                 # Static assets
│   ├── .env.local.example      # NEXT_PUBLIC_API_URL, NEXT_PUBLIC_WS_URL
│   ├── next.config.ts          # Next.js config (Tailwind via Turbopack loader)
│   ├── eslint.config.mjs
│   ├── tsconfig.json
│   └── package.json
│
├── api/                        # Synchronous FastAPI service
│   ├── main.py                 # App entrypoint & lifecycle
│   ├── routes/
│   │   ├── tenants.py          # Onboarding, tenant profile, key rotation
│   │   ├── documents.py        # Upload, list/status, delete, re-index
│   │   └── chat_ws.py          # Authenticated WebSocket chat handler
│   ├── validators/
│   │   └── file_type.py        # Byte-level MIME sniffing & allowlist
│   └── middleware/
│       ├── auth.py             # SHA-256 hashed API key validation
│       └── rate_limit.py       # Redis token bucket
│
├── core/                       # Shared configs and connectors
│   ├── config.py               # Pydantic Settings
│   ├── db/
│   │   ├── postgres.py         # SQLAlchemy / asyncpg
│   │   └── redis.py            # Redis client
│   └── models/
│       ├── schema.py           # Relational schema
│       └── events.py           # RabbitMQ job & status payloads
│
├── storage/                    # Storage adapters
│   ├── blob.py                 # BlobStorage interface + S3-API adapter (MinIO or AWS S3)
│   └── vector_db.py            # Qdrant client (with tenant pre-filtering)
│
├── workers/                    # Ingestion pipeline worker
│   ├── consumer.py             # RabbitMQ consumer loop with DLQ fallback
│   ├── extractor.py            # Routing by detected MIME + direct extraction to Markdown
│   ├── ocr.py                  # LightOn OCR for images & scanned PDFs
│   ├── chunker.py              # Markdown-aware structural splitter
│   ├── embedder.py             # Batched text embedding
│   └── publisher.py            # RabbitMQ document status publisher
│
├── tests/                      # pytest suite
│   └── storage/
│       └── test_blob.py        # Blob adapter tests (integration tests run against MinIO)
│
├── docker-compose.yml          # Local infra (currently MinIO + bucket init)
├── .env.example                # Sample environment configurations
├── pyproject.toml              # pytest configuration
├── requirements.txt            # Python dependencies
└── requirements-dev.txt        # Dev/test dependencies (pytest)
```

---

## 🚀 Quickstart

### 1. Prerequisites
* Python 3.11+ (tested on 3.15)
* Node.js 20+ (for the Next.js frontend)
* Docker & Docker Compose
* NVIDIA GPU (optional; only needed to run LightOn OCR locally on images and scanned PDFs)

### 2. Infrastructure Setup
Clone the repository and boot the local service stack:

```bash
git clone https://github.com/<your-username>/rag-vector-engine.git
cd rag-vector-engine

# Copy environment variables
cp .env.example .env

# Start local infrastructure (currently MinIO; Postgres, Redis, Qdrant,
# and RabbitMQ are added to docker-compose.yml as they are built)
docker compose up -d
```

The `minio-init` container creates the `documents` bucket and exits. The MinIO console is at http://localhost:9001 (`minioadmin` / `minioadmin`).

### 3. Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For development and tests, install `requirements-dev.txt` instead and run the suite (MinIO integration tests are skipped automatically if MinIO isn't running):
```bash
pip install -r requirements-dev.txt
pytest -v
```

### 4. Run Migrations & Start Services
Terminal 1 — API Gateway:
```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

Terminal 2 — Ingestion Worker:
```bash
python -m workers.consumer
```

Terminal 3 — Frontend (Next.js + Tailwind CSS):
```bash
cd client
cp .env.local.example .env.local
npm install
npm run dev
```

Open http://localhost:3000 to onboard a tenant, upload documents, and chat.

---

## 🔌 API & Integration Guide

### 1. Onboard a Business
Generate a scoped API key and tenant partition.

```bash
curl -X POST http://localhost:8000/v1/tenants/onboard \
  -H "Content-Type: application/json" \
  -d '{"business_name": "Acme Corp", "contact_email": "admin@acme.com"}'
```

**Response:**
```json
{
  "business_id": "biz_98a72b1c",
  "api_key": "rve_live_8f04c6a461bdfa3754e",
  "message": "Store this API key safely; it cannot be retrieved again."
}
```

---

### 2. Ingest Business Documents
Upload PDF, DOCX, Excel, CSV, or image files into the tenant's isolated ingestion pipeline. Text-based files are extracted directly; images and scanned PDFs go through LightOn OCR.

```bash
curl -X POST http://localhost:8000/v1/documents/upload \
  -H "Authorization: Bearer rve_live_8f04c6a461bdfa3754e" \
  -F "file=@quarterly_report.pdf"
```

**Response:**
```json
{
  "doc_id": "doc_e4c3a102",
  "status": "QUEUED",
  "filename": "quarterly_report.pdf",
  "detected_mime": "application/pdf",
  "minio_key": "biz_98a72b1c/doc_e4c3a102/source.pdf"
}
```

If the file's bytes don't match an allowed type (for example, an `.exe` renamed to `report.pdf`), the upload is rejected before it is stored or queued:

**Response (`415 Unsupported Media Type`):**
```json
{
  "detail": "Unsupported or mismatched file type",
  "detected_mime": "application/x-dosexec"
}
```

---

### 3. Track Document Status (Polling)
There is no push channel for ingestion status; clients poll the document list (the dashboard does this every few seconds while any document is `QUEUED` or `PROCESSING`).

```bash
curl http://localhost:8000/v1/documents \
  -H "Authorization: Bearer rve_live_8f04c6a461bdfa3754e"
```

**Response:**
```json
{
  "documents": [
    {
      "doc_id": "doc_e4c3a102",
      "filename": "quarterly_report.pdf",
      "status": "INDEXED",
      "detected_mime": "application/pdf",
      "updated_at": "2026-10-29T11:42:07Z"
    }
  ]
}
```

A single document can be fetched with `GET /v1/documents/{doc_id}`.

---

### 4. Delete a Document
Removes the document's vectors from Qdrant (filtered by `business_id` **and** `doc_id`), its objects under `{business_id}/{doc_id}/` in MinIO, and its PostgreSQL record.

```bash
curl -X DELETE http://localhost:8000/v1/documents/doc_e4c3a102 \
  -H "Authorization: Bearer rve_live_8f04c6a461bdfa3754e"
```

**Response:** `204 No Content`

---

### 5. Re-index a Document
Deletes the document's existing vectors and re-queues an ingestion job from the stored source file. The status goes back to `QUEUED`.

```bash
curl -X POST http://localhost:8000/v1/documents/doc_e4c3a102/reindex \
  -H "Authorization: Bearer rve_live_8f04c6a461bdfa3754e"
```

**Response:**
```json
{
  "doc_id": "doc_e4c3a102",
  "status": "QUEUED"
}
```

Deleting or re-indexing a document that is currently `QUEUED` or `PROCESSING` returns `409 Conflict`, so an in-flight job can't write vectors for a document that was just removed.

---

### 6. Query via Authenticated WebSocket
Connect client applications to the real-time inference gateway. The API key is passed as the `token` query parameter. The server resolves the tenant from the key and closes the connection if it doesn't match the `{business_id}` in the path.

```javascript
const businessId = "biz_98a72b1c";
const apiKey = "rve_live_8f04c6a461bdfa3754e";

const socket = new WebSocket(`ws://localhost:8000/v1/chat/ws/${businessId}?token=${apiKey}`);

socket.onopen = () => {
  // Submit customer question
  socket.send(JSON.stringify({
    query: "What were our total operating revenues for Q3?",
    session_id: "client_session_442"
  }));
};

socket.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === "delta") {
    process.stdout.write(data.content); // Streamed token
  } else if (data.type === "done") {
    console.log("\nSources:", data.sources);
  }
};
```

---

## 🪣 Object Storage (MinIO now, AWS S3 later)

Raw files are stored through a single adapter, `storage/blob.py`. Callers depend only on the `BlobStorage` interface (`put_object`, `get_object`, `head_object`, `exists`, `delete_object`, `delete_prefix`, `ensure_bucket`), obtained via `get_blob_storage()`.

The implementation, `S3BlobStorage`, talks the S3 API (`aioboto3`). MinIO is S3-compatible, so **switching between local MinIO and AWS S3 is configuration only, with no code changes**:

| Variable | Local MinIO | AWS S3 |
|----------|-------------|--------|
| `S3_ENDPOINT_URL` | `http://localhost:9000` | empty |
| `S3_FORCE_PATH_STYLE` | `true` | `false` |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | MinIO root credentials | IAM keys, or empty to use the default AWS credential chain (IAM role, profile) |
| `S3_REGION` | `us-east-1` | your bucket's region |
| `S3_BUCKET` | `documents` | your bucket name |

Object keys are always built with `object_key(business_id, doc_id, ext)` → `{business_id}/{doc_id}/source.{ext}`, which rejects any ID or extension that could escape the tenant prefix.

> **MinIO image:** the official `minio/minio` images are no longer published on Docker Hub, so `docker-compose.yml` uses the community-maintained build [`pgsty/minio`](https://hub.docker.com/r/pgsty/minio), pinned to a specific release.

---

## 🔍 Upload Validation

Uploaded documents are treated as untrusted input. The client-supplied `Content-Type` header and file extension are **never** trusted. Instead, `api/validators/file_type.py` inspects the raw bytes of the file (magic-number sniffing via `libmagic` / `python-magic`) and only accepts types on the allowlist:

| Type | Detected MIME | Byte-level check |
|------|---------------|------------------|
| PDF | `application/pdf` | Starts with `%PDF-` (`25 50 44 46 2D`) |
| PNG | `image/png` | Starts with `89 50 4E 47 0D 0A 1A 0A` |
| JPEG | `image/jpeg` | Starts with `FF D8 FF` |
| TIFF | `image/tiff` | Starts with `49 49 2A 00` or `4D 4D 00 2A` |
| DOCX | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | ZIP header `50 4B 03 04` **and** archive contains `word/document.xml` |
| XLSX | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | ZIP header `50 4B 03 04` **and** archive contains `xl/workbook.xml` |
| CSV | `text/csv` | No magic bytes: must decode as UTF-8, contain no NUL bytes, and parse as delimited rows |

Validation rules:

1. **Detected type wins.** The MIME type detected from the bytes is the only one used downstream. It is stored in PostgreSQL and passed in the RabbitMQ job payload, and the worker routes extraction/OCR by it.
2. **Reject on mismatch.** Anything not on the allowlist, or whose bytes contradict the declared type, is rejected with `415` before it reaches MinIO or the queue.
3. **Inspect ZIP containers.** DOCX and XLSX are ZIP archives, so the archive's entries are checked to tell them apart from arbitrary ZIPs. Macro-enabled documents (containing `vbaProject.bin`) are rejected, and total uncompressed size is capped to block zip bombs.
4. **Size limit.** Uploads above a configured maximum size are rejected before sniffing.
5. **Safe storage keys.** The original filename is kept only as metadata; object keys are built from server-generated IDs so user input can't cause path traversal.

---

## 🛡️ Tenant Isolation & Security Guarantees

1. **Object Storage:** Documents are stored using strict tenant prefixes: `s3://bucket/{business_id}/{doc_id}/source.{ext}`, where `ext` comes from the detected MIME type, not the uploaded filename.
2. **Vector Space:** Chunks are tagged with a non-bypassable `business_id` payload field. All vector retrieval queries execute an indexed exact-match pre-filter:
   ```python
   models.Filter(
       must=[
           models.FieldCondition(
               key="business_id",
               match=models.MatchValue(value=authenticated_business_id)
           )
       ]
   )
   ```
   `business_id` (and `doc_id`, used for deletion and re-indexing) are created as keyword payload indexes on the collection, so the pre-filter is an index lookup rather than a scan.
3. **API Keys:** Plaintext API keys are never stored; only high-entropy `SHA-256` hashes are persisted and verified.
4. **Tenant Comes from the Key:** The tenant is always resolved from the API key. On the WebSocket route, a `{business_id}` path value that doesn't match the key's tenant is rejected.
5. **Untrusted Uploads:** File types are validated from raw bytes, never from headers or extensions; see [Upload Validation](#-upload-validation).
6. **Chat History Retention:** Stored chat turns are scoped to the tenant and deleted after 7 days.

---

## 📜 License
Apache-2.0 © 2026 rag-vector-engine contributors.