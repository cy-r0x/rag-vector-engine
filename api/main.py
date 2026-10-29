from fastapi import Depends, FastAPI

from api.middleware.auth import JWTAuthMiddleware, public, require_auth
from api.middleware.request_logger import RequestLoggingMiddleware
from api.routes.routes import router
from core.config import get_settings
from core.logging import setup_logging
from core.tokens import get_jwt_codec

setup_logging(get_settings().log_level)

app = FastAPI(title="api", dependencies=[Depends(require_auth)])
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
