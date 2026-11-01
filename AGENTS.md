# AGENTS.md

Multi-tenant RAG-as-a-service backend (FastAPI API + RabbitMQ worker + Postgres + MinIO + Qdrant + Gemini).

## Start here

1. Read [`.cursor/index.md`](.cursor/index.md): system overview, code map, security rules, conventions, settings,
   and how to run everything.
2. Check [`.cursor/memory/`](.cursor/memory/) before designing anything:
   - `decisions.md`: choices already made with the user (and why); don't silently reverse them.
   - `lessons.md`: gotchas found the hard way (aio-pika, Gemini, Qdrant, FastAPI WebSockets).
   - `status.md`: what works today, known gaps, next steps.
3. Follow [`.cursor/rules/ask-when-unclear.mdc`](.cursor/rules/ask-when-unclear.mdc): ask (AskQuestion, recommended
   option first) when a request is ambiguous, touches architecture/dependencies, or is destructive.

## Working agreements

- `business_id` comes only from verified tokens; every vector query is pre-filtered by it.
- External providers stay behind `infra.embeddings` / `infra.llm` protocols; never import a provider SDK elsewhere.
- New settings go in `common/config/config.py` and `.env.example`; never commit `.env`.
- Schema changes get a new Alembic revision; committed migrations are not edited.
- Keep `pytest -q` green, and update the README, `.cursor/index.md` and `.cursor/memory/` when behaviour changes.
- The worker doesn't auto-reload: restart `python -m workers.consumer` after changing worker code.
