import logging
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core.tokens import JWTCodec, TokenError

logger = logging.getLogger("api.auth")

PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        "/v1/auth/register",
        "/v1/auth/login",
    }
)


class JWTAuthMiddleware:
    """Verifies `Authorization: Bearer <jwt>` and exposes the decoded claims as `request.state.token_payload`.

    Only HTTP requests are checked; WebSocket connections must authenticate in their route.
    """

    def __init__(self, app: ASGIApp, *, codec: JWTCodec, public_paths: Iterable[str] = PUBLIC_PATHS) -> None:
        self.app = app
        self.codec = codec
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS" or scope["path"] in self.public_paths:
            await self.app(scope, receive, send)
            return

        token = _bearer_token(Headers(scope=scope).get("authorization"))
        if token is None:
            await _reject(scope, receive, send, "Missing or malformed Authorization header")
            return

        try:
            payload = self.codec.decode(token)
        except TokenError as exc:
            await _reject(scope, receive, send, str(exc))
            return

        scope.setdefault("state", {})["token_payload"] = payload
        await self.app(scope, receive, send)


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def _reject(scope: Scope, receive: Receive, send: Send, reason: str) -> None:
    logger.warning("Rejected %s %s: %s", scope["method"], scope["path"], reason)
    response = JSONResponse(
        {"detail": reason},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )
    await response(scope, receive, send)


def get_token_payload(request: Request) -> dict[str, Any]:
    payload = getattr(request.state, "token_payload", None)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload
