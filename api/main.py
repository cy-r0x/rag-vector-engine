from fastapi import Depends, FastAPI

from api.middleware.auth import JWTAuthMiddleware, require_auth
from api.middleware.request_logger import RequestLoggingMiddleware
from api.routes.routes import router
from common.auth import get_jwt_codec
from common.config import get_settings
from common.decorators import public
from common.logger import setup_logging

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
