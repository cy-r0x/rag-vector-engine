from fastapi import FastAPI

from api.middleware.auth import JWTAuthMiddleware
from core.tokens import get_jwt_codec

app = FastAPI(title="rag-vector-engine")
app.add_middleware(JWTAuthMiddleware, codec=get_jwt_codec())


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "rag-vector-engine", "status": "ok"}
