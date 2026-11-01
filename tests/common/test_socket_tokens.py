import hashlib

from common.auth import SOCKET_TOKEN_PREFIX, generate_socket_token, hash_socket_token, is_socket_token


def test_generated_tokens_are_prefixed_random_and_hashed():
    first, second = generate_socket_token(), generate_socket_token()

    assert first.value.startswith(SOCKET_TOKEN_PREFIX)
    assert first.value != second.value
    assert len(first.value) >= len(SOCKET_TOKEN_PREFIX) + 43
    assert first.hash == hashlib.sha256(first.value.encode()).hexdigest() == hash_socket_token(first.value)
    assert first.prefix == first.value[: len(SOCKET_TOKEN_PREFIX) + 6]


def test_socket_tokens_are_told_apart_from_jwts():
    assert is_socket_token(generate_socket_token().value)
    assert not is_socket_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.sig")
