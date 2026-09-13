"""Tests for the backend health check and root endpoints."""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_app_instance():
    """Verify that the FastAPI app instance is created properly."""
    assert app is not None
    assert app.title == "ARIES Crash Recovery System API"


def test_health_check_status_code():
    """Verify that GET /api/health returns HTTP 200 OK."""
    response = client.get("/api/health")
    assert response.status_code == 200


def test_health_check_payload():
    """Verify the structure and contents of GET /api/health response."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert data.get("status") == "ok"
    assert data.get("service") == "ARIES Crash Recovery System"
    assert "version" in data


def test_root_endpoint():
    """Verify that GET / returns service information."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data.get("service") == "ARIES Crash Recovery System"
    assert data.get("status") == "online"
