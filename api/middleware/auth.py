import logging
from typing import Any

from fastapi import HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from common.auth import JWTCodec, TokenError
from common.decorators import is_public

logger = logging.getLogger("api.auth")

_MISSING_TOKEN = "Missing or malformed Authorization header"


class JWTAuthMiddleware:
    """Decodes `Authorization: Bearer <jwt>` and attaches the claims as `request.state.token_payload`.

    It never rejects on its own: enforcement happens in `require_auth`, which runs after
    routing and knows whether the endpoint is `@public`. A token that fails verification
    leaves its reason in `request.state.auth_error`.
    """

    def __init__(self, app: ASGIApp, *, codec: JWTCodec) -> None:
        self.app = app
        self.codec = codec

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            token = _bearer_token(Headers(scope=scope).get("authorization"))
            if token is not None:
                try:
                    state["token_payload"] = self.codec.decode(token)
                except TokenError as exc:
                    state["auth_error"] = str(exc)
        await self.app(scope, receive, send)


def require_auth(request: Request) -> None:
    """App-wide dependency: requires a valid token unless the matched endpoint is `@public`."""
    route = request.scope.get("route")
    if is_public(getattr(route, "endpoint", None)):
        return
    if getattr(request.state, "token_payload", None) is not None:
        return

    reason = getattr(request.state, "auth_error", None) or _MISSING_TOKEN
    logger.warning("Rejected %s %s: %s", request.method, request.url.path, reason)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=reason,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_token_payload(request: Request) -> dict[str, Any]:
    payload = getattr(request.state, "token_payload", None)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
