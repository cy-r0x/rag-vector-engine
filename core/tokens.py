import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import jwt

from core.config import get_settings

REQUIRED_CLAIMS = ("exp", "sub", "business_id")
RESERVED_CLAIMS = frozenset({"exp", "iat", "sub", "business_id"})
MIN_SECRET_LENGTH = 32


class TokenError(Exception):
    pass


class TokenExpiredError(TokenError):
    pass


class JWTCodec:
    """Signs and verifies access tokens (HS256 by default)."""

    def __init__(self, *, secret: str, algorithm: str = "HS256", expires_in: int = 3600) -> None:
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"JWT secret must be at least {MIN_SECRET_LENGTH} characters")
        self._secret = secret
        self.algorithm = algorithm
        self.expires_in = expires_in

    def encode(
        self,
        *,
        sub: str,
        business_id: str,
        expires_in: int | None = None,
        extra_claims: Mapping[str, Any] | None = None,
    ) -> str:
        if extra_claims and RESERVED_CLAIMS & extra_claims.keys():
            raise ValueError(f"extra_claims may not set reserved claims: {sorted(RESERVED_CLAIMS)}")
        now = int(time.time())
        lifetime = self.expires_in if expires_in is None else expires_in
        claims = {
            **(extra_claims or {}),
            "sub": sub,
            "business_id": business_id,
            "iat": now,
            "exp": now + lifetime,
        }
        return jwt.encode(claims, self._secret, algorithm=self.algorithm)

    def decode(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=[self.algorithm],
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("Token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError("Invalid token") from exc


@lru_cache
def get_jwt_codec() -> JWTCodec:
    settings = get_settings()
    return JWTCodec(
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_in=settings.jwt_expires_in,
    )
