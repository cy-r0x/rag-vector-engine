import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from api.consumers import run_document_events_consumer
from api.middleware.auth import JWTAuthMiddleware, require_auth
from api.middleware.request_logger import RequestLoggingMiddleware
from api.routes.routes import router
from common.auth import get_jwt_codec
from common.config import get_settings
from common.decorators import public
from common.logger import setup_logging
from db import dispose_engine
from infra.embeddings import get_embedder
from infra.llm import get_chat_model
from infra.queue import close_job_publisher
from infra.storage.vector_db import get_vector_store

setup_logging(get_settings().log_level)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    stop = asyncio.Event()
    events_consumer = (
        asyncio.create_task(run_document_events_consumer(stop)) if get_settings().consume_document_events else None
    )
    yield
    stop.set()
    if events_consumer is not None:
        await events_consumer
    await close_job_publisher()
    for cached in (get_embedder, get_vector_store, get_chat_model):
        if cached.cache_info().currsize:
            await cached().close()
    await dispose_engine()


app = FastAPI(title="api", dependencies=[Depends(require_auth)], lifespan=lifespan)
app.add_middleware(JWTAuthMiddleware, codec=get_jwt_codec())
app.add_middleware(RequestLoggingMiddleware)
app.include_router(router)


@app.get("/health")
@public
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=True)
