"""Static per-business tokens for server-to-server chat sockets.

Tokens are 256-bit random values with a recognisable prefix. Only a SHA-256 hash is stored: the token has full
entropy, so a fast hash is enough (no password-style stretching), and lookups by hash need no comparison loop.
"""

import hashlib
import secrets
from dataclasses import dataclass

SOCKET_TOKEN_PREFIX = "rve_sk_"
_RANDOM_BYTES = 32
_DISPLAY_CHARS = len(SOCKET_TOKEN_PREFIX) + 6


@dataclass(frozen=True)
class NewSocketToken:
    value: str
    hash: str
    prefix: str


def generate_socket_token() -> NewSocketToken:
    value = SOCKET_TOKEN_PREFIX + secrets.token_urlsafe(_RANDOM_BYTES)
    return NewSocketToken(value=value, hash=hash_socket_token(value), prefix=value[:_DISPLAY_CHARS])


def hash_socket_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_socket_token(token: str) -> bool:
    return token.startswith(SOCKET_TOKEN_PREFIX)
