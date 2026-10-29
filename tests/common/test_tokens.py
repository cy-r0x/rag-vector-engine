import time

import jwt
import pytest

from core.tokens import JWTCodec, TokenError, TokenExpiredError, get_jwt_codec

SECRET = "unit-test-secret-that-is-at-least-32-chars"


@pytest.fixture
def codec() -> JWTCodec:
    return JWTCodec(secret=SECRET, expires_in=600)


def test_encode_decode_roundtrip(codec: JWTCodec):
    before = int(time.time())
    payload = codec.decode(codec.encode(sub="user_1", business_id="biz_1"))

    assert payload["sub"] == "user_1"
    assert payload["business_id"] == "biz_1"
    assert before <= payload["iat"] <= int(time.time())
    assert payload["exp"] == payload["iat"] + 600


def test_custom_expires_in(codec: JWTCodec):
    payload = codec.decode(codec.encode(sub="u", business_id="b", expires_in=30))
    assert payload["exp"] - payload["iat"] == 30


def test_extra_claims_are_included(codec: JWTCodec):
    payload = codec.decode(codec.encode(sub="u", business_id="b", extra_claims={"role": "admin"}))
    assert payload["role"] == "admin"


@pytest.mark.parametrize("claim", ["sub", "business_id", "exp", "iat"])
def test_extra_claims_cannot_override_reserved(codec: JWTCodec, claim: str):
    with pytest.raises(ValueError):
        codec.encode(sub="u", business_id="b", extra_claims={claim: "x"})


def test_expired_token_raises(codec: JWTCodec):
    token = codec.encode(sub="u", business_id="b", expires_in=-10)
    with pytest.raises(TokenExpiredError):
        codec.decode(token)


def test_token_signed_with_other_secret_raises(codec: JWTCodec):
    other = JWTCodec(secret="another-secret-that-is-at-least-32-chars!")
    with pytest.raises(TokenError):
        codec.decode(other.encode(sub="u", business_id="b"))


def test_tampered_token_raises(codec: JWTCodec):
    header, payload, signature = codec.encode(sub="u", business_id="b").split(".")
    tampered = f"{header}.{payload}.{signature[:-2]}xx"
    with pytest.raises(TokenError):
        codec.decode(tampered)


@pytest.mark.filterwarnings("ignore:The HMAC key")
def test_token_with_different_algorithm_raises(codec: JWTCodec):
    token = jwt.encode(
        {"sub": "u", "business_id": "b", "exp": int(time.time()) + 60}, SECRET, algorithm="HS512"
    )
    with pytest.raises(TokenError):
        codec.decode(token)


def test_short_secret_is_refused():
    with pytest.raises(ValueError):
        JWTCodec(secret="too-short")


def test_get_jwt_codec_uses_settings():
    codec = get_jwt_codec()
    assert codec.decode(codec.encode(sub="u", business_id="b"))["business_id"] == "b"
