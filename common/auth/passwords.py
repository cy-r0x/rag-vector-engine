from functools import lru_cache

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordHasher:
    """Argon2id password hashing."""

    def __init__(self) -> None:
        self._hasher = _Argon2Hasher()
        self._dummy_hash = self._hasher.hash("dummy-password-for-timing")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def verify_dummy(self, password: str) -> None:
        """Spend the same time as a real verify, so unknown emails can't be told apart by latency."""
        self.verify(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


@lru_cache
def get_password_hasher() -> PasswordHasher:
    return PasswordHasher()
