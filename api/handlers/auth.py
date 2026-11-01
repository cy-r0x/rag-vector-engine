from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from common.auth import get_jwt_codec, get_password_hasher
from common.decorators import public
from db import get_session
from db.models import Business, User

_INVALID_CREDENTIALS = "Invalid email or password"

Password = Annotated[str, Field(min_length=8, max_length=128)]


class RegisterRequest(BaseModel):
    business_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: EmailStr
    password: Password


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class TokenResponse(BaseModel):
    business_id: str
    user_id: str
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


@public
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    """Onboard a business together with its first (owner) user."""
    password_hash = await run_in_threadpool(get_password_hasher().hash, body.password)
    business = Business(name=body.business_name)
    user = User(business=business, email=body.email.lower(), password_hash=password_hash, role="owner")
    session.add_all([business, user])
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered") from None
    return _issue_token(user)


@public
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    hasher = get_password_hasher()
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None:
        await run_in_threadpool(hasher.verify_dummy, body.password)
        raise _unauthorized()
    if not await run_in_threadpool(hasher.verify, user.password_hash, body.password):
        raise _unauthorized()

    if hasher.needs_rehash(user.password_hash):
        user.password_hash = await run_in_threadpool(hasher.hash, body.password)
        await session.commit()
    return _issue_token(user)


def _issue_token(user: User) -> TokenResponse:
    codec = get_jwt_codec()
    return TokenResponse(
        business_id=user.business_id,
        user_id=user.id,
        access_token=codec.encode(sub=user.id, business_id=user.business_id),
        expires_in=codec.expires_in,
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_INVALID_CREDENTIALS,
        headers={"WWW-Authenticate": "Bearer"},
    )
