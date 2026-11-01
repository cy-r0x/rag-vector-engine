import logging
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from common.auth import JWTCodec, TokenError
from common.decorators import is_public

logger = logging.getLogger("api.auth")

_MISSING_TOKEN = "Missing or malformed Authorization header"

# Claim marking a purpose-bound token (e.g. a chat WebSocket ticket) that must not be accepted as an access token.
TOKEN_USE_CLAIM = "token_use"


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
                    payload = self.codec.decode(token)
                except TokenError as exc:
                    state["auth_error"] = str(exc)
                else:
                    if TOKEN_USE_CLAIM in payload:
                        state["auth_error"] = "Token is not valid for this API"
                    else:
                        state["token_payload"] = payload
        await self.app(scope, receive, send)


def require_auth(connection: HTTPConnection) -> None:
    """App-wide dependency: requires a valid token unless the matched endpoint is `@public`.

    WebSocket endpoints are `@public` and authenticate the connection themselves.
    """
    route = connection.scope.get("route")
    if is_public(getattr(route, "endpoint", None)):
        return
    if getattr(connection.state, "token_payload", None) is not None:
        return

    reason = getattr(connection.state, "auth_error", None) or _MISSING_TOKEN
    logger.warning("Rejected %s %s: %s", connection.scope.get("method", "WS"), connection.url.path, reason)
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


def get_business_id(payload: dict[str, Any] = Depends(get_token_payload)) -> UUID:
    """The tenant of the authenticated request, parsed from the token's `business_id` claim."""
    try:
        return UUID(payload["business_id"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
