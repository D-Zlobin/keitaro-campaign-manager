from fastapi.testclient import TestClient

from app.config import Settings


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_exposes_no_secrets(client: TestClient, settings: Settings) -> None:
    response = client.get("/api/v1/health")
    assert settings.keitaro_api_key.get_secret_value() not in response.text
    assert settings.database_url not in response.text
