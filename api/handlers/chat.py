"""Per-business chat over WebSocket: `/v1/chat/ws/{business_id}`.

Credentials, in an `Authorization: Bearer` header or the `token` query parameter: the business's static socket
token (server-to-server), a chat ticket (dashboard), or an access token.

Client → server: `{"query": "...", "session_id": "optional"}`.
Server → client, per query: zero or more `{"type": "delta", "content": "..."}` frames with the streamed answer, then
`{"type": "done", "sources": [...]}`; or `{"type": "error", "detail": "..."}`. `session_id` is echoed in every frame.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.middleware.auth import TOKEN_USE_CLAIM, get_token_payload
from api.rag import NO_CONTEXT_ANSWER, answer, retrieve
from common.auth import TokenError, get_jwt_codec, hash_socket_token, is_socket_token
from common.config import get_settings
from common.decorators import public
from db import get_sessionmaker
from db.models import SocketToken
from infra.embeddings import Embedder, EmbeddingError, get_embedder
from infra.llm import ChatModel, ChatModelError, get_chat_model
from infra.storage.vector_db import VectorStore, get_vector_store

logger = logging.getLogger("api.chat")

CHAT_TICKET_USE = "chat_ws"
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403

_SESSION_EXP_CLAIM = "session_exp"
_MAX_SESSION_ID_CHARS = 128


class ChatTicket(BaseModel):
    ticket: str
    business_id: UUID
    expires_in: int
    ws_path: str
    token_type: Literal["chat_ws"] = "chat_ws"


async def create_chat_ticket(payload: dict[str, Any] = Depends(get_token_payload)) -> ChatTicket:
    """A short-lived token that only opens the business's chat socket, so browsers never handle the access token.

    The socket stays usable until the access token it was issued from would have expired.
    """
    settings = get_settings()
    business_id = payload["business_id"]
    ticket = get_jwt_codec().encode(
        sub=payload["sub"],
        business_id=business_id,
        expires_in=settings.chat_ticket_expires_in,
        extra_claims={TOKEN_USE_CLAIM: CHAT_TICKET_USE, _SESSION_EXP_CLAIM: payload["exp"]},
    )
    return ChatTicket(
        ticket=ticket,
        business_id=business_id,
        expires_in=settings.chat_ticket_expires_in,
        ws_path=f"/v1/chat/ws/{business_id}",
    )


@public
async def chat_socket(
    websocket: WebSocket,
    business_id: UUID,
    token: str | None = Query(default=None),
    embedder: Embedder = Depends(get_embedder),
    store: VectorStore = Depends(get_vector_store),
    chat_model: ChatModel = Depends(get_chat_model),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> None:
    await websocket.accept()

    credential = _bearer_token(websocket.headers.get("authorization")) or token
    auth, reason = await _authenticate(credential, sessionmaker)
    if auth is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason=reason)
        return
    # The path names the business to search; it is only trusted once it matches the verified token.
    if auth.business_id != business_id:
        logger.warning("Rejected chat socket for business %s: token belongs to another business", business_id)
        await websocket.close(code=CLOSE_FORBIDDEN, reason="Token does not belong to this business")
        return

    settings = get_settings()
    logger.info("Chat socket opened for business %s (%s)", business_id, auth.kind)
    try:
        while True:
            raw = await websocket.receive_text()
            revoked = await _revocation_reason(auth, sessionmaker)
            if revoked is not None:
                await websocket.send_json({"type": "error", "detail": revoked})
                await websocket.close(code=CLOSE_UNAUTHORIZED, reason=revoked)
                return

            message, error = _parse(raw, settings.chat_max_query_chars)
            if message is None:
                await websocket.send_json({"type": "error", "detail": error})
                continue

            await _respond(
                websocket,
                business_id=business_id,
                query=message["query"],
                session_id=message.get("session_id"),
                embedder=embedder,
                store=store,
                chat_model=chat_model,
                sessionmaker=sessionmaker,
            )
    except WebSocketDisconnect:
        logger.info("Chat socket closed for business %s", business_id)


async def _respond(
    websocket: WebSocket,
    *,
    business_id: UUID,
    query: str,
    session_id: str | None,
    embedder: Embedder,
    store: VectorStore,
    chat_model: ChatModel,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    def frame(**fields: Any) -> dict[str, Any]:
        return {**fields, "session_id": session_id} if session_id is not None else fields

    settings = get_settings()
    try:
        sources = await retrieve(
            business_id=business_id,
            query=query,
            embedder=embedder,
            store=store,
            sessionmaker=sessionmaker,
            top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score,
        )
        logger.info("Chat query for business %s: %d sources above %.2f", business_id, len(sources), settings.retrieval_min_score)

        if not sources:
            await websocket.send_json(frame(type="delta", content=NO_CONTEXT_ANSWER))
        else:
            async for text in answer(chat_model, query, sources):
                await websocket.send_json(frame(type="delta", content=text))
        await websocket.send_json(frame(type="done", sources=[source.to_frame() for source in sources]))
    except WebSocketDisconnect:
        raise
    except (EmbeddingError, ChatModelError) as exc:
        logger.warning("Chat query failed for business %s: %s", business_id, exc)
        await websocket.send_json(frame(type="error", detail="The answer could not be generated. Please try again."))
    except Exception:
        logger.exception("Unexpected chat error for business %s", business_id)
        await websocket.send_json(frame(type="error", detail="Something went wrong. Please try again."))


@dataclass(frozen=True)
class _SocketAuth:
    business_id: UUID
    kind: Literal["access token", "chat ticket", "socket token"]
    session_exp: float | None = None
    socket_token_hash: str | None = None


async def _authenticate(
    credential: str | None, sessionmaker: async_sessionmaker[AsyncSession]
) -> tuple[_SocketAuth | None, str]:
    """Accepts the business's static socket token, a chat ticket, or an access token."""
    if not credential:
        return None, "Missing token"

    if is_socket_token(credential):
        token_hash = hash_socket_token(credential)
        business_id = await _socket_token_business(sessionmaker, token_hash)
        if business_id is None:
            return None, "Invalid or rotated socket token"
        return _SocketAuth(business_id=business_id, kind="socket token", socket_token_hash=token_hash), ""

    try:
        claims = get_jwt_codec().decode(credential)
        business_id = UUID(claims["business_id"])
    except (TokenError, ValueError) as exc:
        return None, str(exc) if isinstance(exc, TokenError) else "Invalid token"
    token_use = claims.get(TOKEN_USE_CLAIM)
    if token_use not in (None, CHAT_TICKET_USE):
        return None, "Token is not valid for chat"
    return (
        _SocketAuth(
            business_id=business_id,
            kind="chat ticket" if token_use == CHAT_TICKET_USE else "access token",
            session_exp=claims.get(_SESSION_EXP_CLAIM, claims["exp"]),
        ),
        "",
    )


async def _revocation_reason(auth: _SocketAuth, sessionmaker: async_sessionmaker[AsyncSession]) -> str | None:
    """Why an open socket may no longer be used: its login session expired, or its socket token was rotated."""
    if auth.session_exp is not None and time.time() >= auth.session_exp:
        return "Your session has expired. Please sign in again."
    if auth.socket_token_hash is not None:
        if await _socket_token_business(sessionmaker, auth.socket_token_hash) != auth.business_id:
            return "This socket token was rotated. Connect again with the new token."
    return None


async def _socket_token_business(sessionmaker: async_sessionmaker[AsyncSession], token_hash: str) -> UUID | None:
    async with sessionmaker() as session:
        return await session.scalar(select(SocketToken.business_id).where(SocketToken.token_hash == token_hash))


def _bearer_token(authorization: str | None) -> str | None:
    scheme, _, value = (authorization or "").partition(" ")
    value = value.strip()
    return value if scheme.lower() == "bearer" and value else None


def _parse(raw: str, max_query_chars: int) -> tuple[dict[str, Any] | None, str]:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return None, 'Messages must be JSON, e.g. {"query": "..."}'
    if not isinstance(message, dict):
        return None, 'Messages must be JSON objects, e.g. {"query": "..."}'

    query = message.get("query")
    if not isinstance(query, str) or not query.strip():
        return None, "Ask a question in the 'query' field"
    if len(query) > max_query_chars:
        return None, f"Questions are limited to {max_query_chars} characters"

    session_id = message.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > _MAX_SESSION_ID_CHARS):
        return None, f"'session_id' must be a string of at most {_MAX_SESSION_ID_CHARS} characters"
    return {"query": query.strip(), "session_id": session_id}, ""
