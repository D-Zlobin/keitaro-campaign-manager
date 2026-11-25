import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import NotFoundError, RuleViolationError
from app.main import create_app


@pytest.fixture
def probe_client(settings: Settings) -> TestClient:
    app = create_app(settings)

    @app.get("/api/v1/_missing")
    async def missing() -> None:
        raise NotFoundError("Campaign not found.", details={"id": 7})

    @app.get("/api/v1/_rules")
    async def rules() -> None:
        raise RuleViolationError("Pinned shares exceed 100.")

    @app.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/no-such-route")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Not Found", "details": {}}
    }


def test_method_not_allowed_uses_error_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_app_error_envelope_and_status(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_missing")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Campaign not found.",
            "details": {"id": 7},
        }
    }

    response = probe_client.get("/api/v1/_rules")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "rule_violation"


def test_unhandled_error_is_generic_and_hides_internals(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/_boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "kaboom" not in response.text
