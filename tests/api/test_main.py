import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"service": "rag-vector-engine", "status": "ok"}


@pytest.mark.parametrize("path", ["/v1/auth/register", "/v1/auth/login"])
def test_auth_routes_are_public_and_not_implemented_yet(path: str):
    response = client.post(path)
    assert response.status_code == 501
