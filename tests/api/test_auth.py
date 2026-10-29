import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.middleware.auth import JWTAuthMiddleware, get_token_payload
from core.tokens import JWTCodec

SECRET = "unit-test-secret-that-is-at-least-32-chars"
CODEC = JWTCodec(secret=SECRET)


def _raw_token(secret: str = SECRET, **overrides) -> str:
    claims = {"sub": "user_1", "business_id": "biz_98a72b1c", "exp": int(time.time()) + 300}
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(JWTAuthMiddleware, codec=CODEC)

    @app.get("/")
    async def root():
        return {"public": True}

    @app.get("/me")
    async def me(payload: dict = Depends(get_token_payload)):
        return payload

    return TestClient(app)


def test_public_path_needs_no_token(client: TestClient):
    assert client.get("/").status_code == 200


def test_valid_token_attaches_payload(client: TestClient):
    token = CODEC.encode(sub="user_1", business_id="biz_98a72b1c")
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "user_1"
    assert body["business_id"] == "biz_98a72b1c"


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic abc",
        "Token abc",
        "Bearer not-a-jwt",
    ],
)
def test_missing_or_malformed_header_is_rejected(client: TestClient, authorization: str | None):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.get("/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_signature_is_rejected(client: TestClient):
    token = _raw_token(secret="another-secret-that-is-at-least-32-chars!")
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_token_is_rejected(client: TestClient):
    token = CODEC.encode(sub="user_1", business_id="biz_1", expires_in=-10)
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"


@pytest.mark.parametrize("missing_claim", ["sub", "business_id", "exp"])
def test_missing_required_claim_is_rejected(client: TestClient, missing_claim: str):
    token = _raw_token(**{missing_claim: None})
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_alg_none_is_rejected(client: TestClient):
    token = jwt.encode(
        {"sub": "user_1", "business_id": "biz_1", "exp": int(time.time()) + 300}, key=None, algorithm="none"
    )
    assert client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_unknown_route_without_token_is_401_not_404(client: TestClient):
    assert client.get("/v1/documents").status_code == 401
