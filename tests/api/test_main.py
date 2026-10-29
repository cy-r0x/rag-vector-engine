from fastapi.testclient import TestClient

from api.main import app


def test_root():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json() == {"service": "rag-vector-engine", "status": "ok"}
