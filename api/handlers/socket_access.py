"""Socket management: a business's chat socket URL and its static socket token (for server-to-server use)."""

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid7

from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.middleware.auth import get_business_id
from common.auth import generate_socket_token
from common.config import get_settings
from db import get_session
from db.models import SocketToken

logger = logging.getLogger("api.socket_access")


class SocketTokenInfo(BaseModel):
    prefix: str
    created_at: datetime


class SocketInfo(BaseModel):
    business_id: UUID
    ws_url: str
    token: SocketTokenInfo | None


class IssuedSocketToken(BaseModel):
    """Returned once: the token itself is never stored and can't be shown again."""

    token: str
    prefix: str
    created_at: datetime
    ws_url: str


def socket_url(business_id: UUID) -> str:
    return f"{get_settings().public_ws_base_url.rstrip('/')}/v1/chat/ws/{business_id}"


async def get_socket_info(
    business_id: UUID = Depends(get_business_id), session: AsyncSession = Depends(get_session)
) -> SocketInfo:
    current = await session.scalar(select(SocketToken).where(SocketToken.business_id == business_id))
    return SocketInfo(
        business_id=business_id,
        ws_url=socket_url(business_id),
        token=SocketTokenInfo(prefix=current.token_prefix, created_at=current.created_at) if current else None,
    )


async def issue_socket_token(
    business_id: UUID = Depends(get_business_id), session: AsyncSession = Depends(get_session)
) -> IssuedSocketToken:
    """Create the business's socket token, or rotate it: the previous token stops working immediately."""
    token = generate_socket_token()
    created_at = datetime.now(UTC)
    values = {"token_hash": token.hash, "token_prefix": token.prefix, "created_at": created_at}
    await session.execute(
        insert(SocketToken)
        .values(id=uuid7(), business_id=business_id, **values)
        .on_conflict_do_update(index_elements=[SocketToken.business_id], set_=values)
    )
    await session.commit()
    logger.info("Issued socket token %s… for business %s", token.prefix, business_id)
    return IssuedSocketToken(
        token=token.value, prefix=token.prefix, created_at=created_at, ws_url=socket_url(business_id)
    )
