import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_is_public():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/v1/auth/register", "/v1/auth/login"])
def test_auth_routes_are_public(path: str):
    response = client.post(path, json={})
    assert response.status_code == 422
