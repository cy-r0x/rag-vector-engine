import time

import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from api.middleware.auth import JWTAuthMiddleware, get_token_payload, public, require_auth
from common.auth import JWTCodec

SECRET = "unit-test-secret-that-is-at-least-32-chars"
CODEC = JWTCodec(secret=SECRET)


def _raw_token(secret: str = SECRET, **overrides) -> str:
    claims = {"sub": "user_1", "business_id": "biz_98a72b1c", "exp": int(time.time()) + 300}
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, secret, algorithm="HS256")


@public
async def public_handler() -> dict:
    return {"public": True}


async def private_handler() -> dict:
    return {"private": True}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(dependencies=[Depends(require_auth)])
    app.add_middleware(JWTAuthMiddleware, codec=CODEC)

    @app.get("/health")
    @public
    async def health():
        return {"public": True}

    @app.get("/items/{item_id}")
    @public
    async def get_item(item_id: int):
        return {"item_id": item_id}

    @app.delete("/items/{item_id}")
    async def delete_item(item_id: int):
        return {"deleted": item_id}

    @app.get("/me")
    async def me(payload: dict = Depends(get_token_payload)):
        return payload

    router = APIRouter(prefix="/v1")
    router.add_api_route("/open", public_handler, methods=["POST"])
    router.add_api_route("/closed", private_handler, methods=["POST"])
    app.include_router(router)

    return TestClient(app)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_public_route_needs_no_token(client: TestClient):
    assert client.get("/health").status_code == 200


def test_public_route_with_path_params(client: TestClient):
    response = client.get("/items/7")
    assert response.status_code == 200
    assert response.json() == {"item_id": 7}


def test_same_path_different_method_is_protected_unless_decorated(client: TestClient):
    assert client.delete("/items/7").status_code == 401
    assert client.delete("/items/7", headers=_auth(CODEC.encode(sub="u", business_id="b"))).status_code == 200


def test_public_decorator_works_with_add_api_route(client: TestClient):
    assert client.post("/v1/open").status_code == 200
    assert client.post("/v1/closed").status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_urls_are_public(client: TestClient, path: str):
    assert client.get(path).status_code == 200


def test_valid_token_attaches_payload(client: TestClient):
    response = client.get("/me", headers=_auth(CODEC.encode(sub="user_1", business_id="biz_98a72b1c")))
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
    assert client.get("/me", headers=_auth(token)).status_code == 401


def test_expired_token_is_rejected(client: TestClient):
    token = CODEC.encode(sub="user_1", business_id="biz_1", expires_in=-10)
    response = client.get("/me", headers=_auth(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"


@pytest.mark.parametrize("missing_claim", ["sub", "business_id", "exp"])
def test_missing_required_claim_is_rejected(client: TestClient, missing_claim: str):
    token = _raw_token(**{missing_claim: None})
    assert client.get("/me", headers=_auth(token)).status_code == 401


def test_alg_none_is_rejected(client: TestClient):
    token = jwt.encode(
        {"sub": "user_1", "business_id": "biz_1", "exp": int(time.time()) + 300}, key=None, algorithm="none"
    )
    assert client.get("/me", headers=_auth(token)).status_code == 401


def test_unknown_route_is_404(client: TestClient):
    assert client.get("/v1/documents").status_code == 404


def test_invalid_token_on_public_route_is_ignored(client: TestClient):
    assert client.get("/health", headers=_auth("not-a-jwt")).status_code == 200


def test_invalid_token_reason_is_returned_on_protected_route(client: TestClient):
    token = CODEC.encode(sub="u", business_id="b", expires_in=-10)
    response = client.delete("/items/1", headers=_auth(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"
